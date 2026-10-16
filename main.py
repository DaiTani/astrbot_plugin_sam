from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.core.message.components import Image
import aiohttp
import base64
import xml.etree.ElementTree as ET
import re
import time
import asyncio
from datetime import datetime, timedelta
import pytz
from typing import Optional

GROUP_GUIDE_TEXT = "为了保护您的隐私，请点击我的头像【添加好友】，并【私信】发送您的【学号或错误截图】，我会为您一对一分析校园网故障。"
PRIVATE_HINT_TEXT = "请发送包含学号或错误信息的图片，我将为您诊断网络问题。"
DIAGNOSIS_WAIT_TEXT = "正在为您识别图片并查询网络数据，请稍候..."
IMAGE_PROCESSING_TEXT = "正在识别图片，请稍候..."

OCR_PROMPT = """请仔细分析这张图片，找出所有文字内容。

重点关注：
1. 学号/用户名（通常是10-12位数字，以202开头或5开头）
2. 错误提示信息（如"连接失败"、"超时"、"账户不存在"等）
3. 状态信息

请完整输出图片中所有可见的文字内容，包括任何数字、字母、符号。如果看到学号，请明确标注。"""

PROBLEM_KEYWORDS = {
    "连接故障": ["无法连接", "断开连接", "没有连接到互联网", "未连接", "连接失败", "无法上网", "上不了网"],
    "服务器问题": ["服务器不可用", "服务器宕机", "HTTP 503", "服务器错误", "服务器无响应"],
    "硬件设备问题": ["路由器无法连接", "连接不稳定", "速度慢", "设备故障"],
    "DNS问题": ["DNS", "域名解析", "无法解析", "DNS错误"],
    "IP地址问题": ["IP地址冲突", "IP冲突", "冲突", "未分配IP", "获取不到IP"],
    "间歇性断网": ["不稳定", "断网", "掉线", "时断时续", "间歇性"],
    "Wi-Fi问题": ["wifi", "wi-fi", "Wi-Fi", "无线", "信号弱", "无法连接无线", "搜不到Wi-Fi", "连不上wifi", "连不上Wi-Fi"],
    "网络中断": ["网络中断", "全面断网", "整栋楼", "线路损坏"],
    "网速慢": ["网速慢", "卡", "卡顿", "延迟高", "速度慢", "网络卡", "卡了", "卡死"],
    "打印机问题": ["打印机", "打印", "无法打印", "打印失败"],
    "路由器问题": ["私接路由器", "多台设备", "路由器", "DHCP", "广播风暴"],
    "证书问题": ["CERT_AUTHORITY_NAVID", "ERROR_AUTHORITY_NAVID", "证书", "连接不是专用连接", "ERR_CERT", "SSL", "你的连接不是专用连接"],
    "账号问题": ["账号已在线", "运营商账号", "强制下线", "账号在线", "同一账号", "多设备登录", "账号被踢"]
}

def _match_problem_category(text: str) -> tuple[str, str]:
    text_lower = text.lower()

    if "套餐" in text or "教工套餐" in text or "免费套餐" in text or "付费" in text:
        return "套餐咨询", "套餐说明"
    if "swr" in text_lower and ("error" in text_lower or "navid" in text_lower):
        return "证书问题", "swrz证书错误"
    if "swrz" in text_lower or "swr.zqle" in text_lower:
        if "error" in text_lower or "navid" in text_lower or "not" in text_lower or "private" in text_lower:
            return "证书问题", "swrz认证页面错误"

    if "账号" in text and "在线" in text:
        return "账号问题", "账号已在线"
    if "运营商" in text and "在线" in text:
        return "账号问题", "运营商账号在线"
    if "强制下线" in text:
        return "账号问题", "强制下线"
    if "账号在线" in text or "在线账号" in text:
        return "账号问题", "账号在线"
    if "测速" in text or "speed" in text_lower:
        return "网速慢", "测速网站"
    if "网速" in text or "速度慢" in text:
        return "网速慢", "网速问题"
    if "认证" in text and "超时" in text:
        return "连接故障", "认证超时"
    if "响应超时" in text or "超时" in text:
        return "连接故障", "响应超时"

    for category, keywords in PROBLEM_KEYWORDS.items():
        for keyword in keywords:
            if keyword.lower() in text_lower:
                return category, keyword

    return "未知问题", ""

def get_message_type_str(event: AstrMessageEvent) -> str:
    msg_type = event.get_message_type()
    if hasattr(msg_type, 'value'):
        return msg_type.value
    return str(msg_type)


class UserDevicesPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self._config = None
        self.user_query_times: dict[str, float] = {}
        self.pending_verification: dict[str, dict] = {}
        self.pending_reply: dict[str, str] = {}
        self.diagnosis_context: dict[str, dict] = {}
        self.pending_users: set[str] = set()
        self.user_selected_type: dict[str, str] = {}
        self.pending_user_type_selection: set[str] = set()
        self.pending_login_log: dict[str, dict] = {}
        self.pending_hint: set[str] = set()
        self.pending_fail_log: dict[str, dict] = {}
        self.pending_diagnosis: dict[str, dict] = {}
        self.pending_image_ocr: dict[str, str] = {}
        self.last_category: dict[str, str] = {}
        self.last_user_message: dict[str, str] = {}
        self.last_image_time: dict[str, float] = {}

    @property
    def config(self) -> dict:
        if self._config is None:
            self._config = self.context.get_config() or {}
        return self._config

    def _get_config(self) -> dict:
        return self.config

    def _is_feature_enabled(self, feature: str) -> bool:
        feature_map = {
            "device": "enable_device_query",
            "login_log": "enable_login_log_query",
            "fail_log": "enable_fail_log_query",
            "network_diagnosis": "enable_network_diagnosis"
        }
        config_key = feature_map.get(feature, "")
        return self.config.get(config_key, True) if config_key else True

    def _check_rate_limit(self, user_id: str) -> int:
        rate_limit = self.config.get("rate_limit_seconds", 60)
        current_time = time.time()
        if user_id in self.user_query_times:
            elapsed = current_time - self.user_query_times[user_id]
            if elapsed < rate_limit:
                return int(rate_limit - elapsed)
        self.user_query_times[user_id] = current_time
        return 0

    def _get_query_days_range(self) -> tuple[str, str]:
        query_days = self.config.get("query_days", 3)
        tz = pytz.utc
        now = datetime.now(tz)
        to_time = now
        from_time = now - timedelta(days=query_days)
        fmt = "%Y-%m-%dT%H:%M:%S.%f"
        return from_time.strftime(fmt)[:-3] + "Z", to_time.strftime(fmt)[:-3] + "Z"

    def validate_account_format(self, account: str) -> tuple[bool, str]:
        if re.match(r'^1043\d{7}$', account):
            return True, "研究生账号"
        elif re.match(r'^5\d{5}$', account):
            return True, "正式编制教师账号"
        elif re.match(r'^H[A-Za-z0-9]{6}$', account):
            return True, "合同工教师账号"
        elif re.match(r'^202[4-9]\d{8}$', account):
            return True, "学生账号"
        return False, "未知账号类型"

    def extract_student_id(self, text: str) -> Optional[str]:
        all_numbers = re.findall(r'\b\d+\b', text)
        valid_ids = []
        for num in all_numbers:
            if len(num) >= 6 and len(num) <= 12:
                is_valid, _ = self.validate_account_format(num)
                if is_valid:
                    valid_ids.append((len(num), num))

        if valid_ids:
            valid_ids.sort(key=lambda x: x[0], reverse=True)
            return valid_ids[0][1]

        partial_ids = [n for n in all_numbers if 6 <= len(n) <= 9]
        for num in partial_ids:
            if num.startswith('58') and len(num) == 6:
                candidate = '202' + num
                is_valid, _ = self.validate_account_format(candidate)
                if is_valid:
                    return candidate

        return None

    def _get_sam_auth(self) -> tuple[str, str]:
        sam_url = self.config.get("sam_url", "https://172.17.21.115:8443/sam/services/samapi")
        admin_user = self.config.get("admin_user", "zzpt")
        admin_pass = self.config.get("admin_pass", "Zzpt@0923")
        return sam_url, admin_user, admin_pass

    async def _sam_request(self, soap_action: str, soap_body: str, timeout: float = 10.0) -> Optional[str]:
        sam_url, admin_user, admin_pass = self._get_sam_auth()
        auth_str = f"{admin_user}:{admin_pass}"
        base64_auth = base64.b64encode(auth_str.encode()).decode('utf-8')

        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": f"Basic {base64_auth}",
            "SOAPAction": soap_action
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    sam_url,
                    data=soap_body,
                    headers=headers,
                    verify_ssl=False,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as response:
                    if response.status != 200:
                        logger.error(f"[SAM] HTTP {response.status} for {soap_action}")
                        return None
                    return await response.text()
        except aiohttp.ClientError as e:
            logger.error(f"[SAM] Connection error for {soap_action}: {e}")
            return None
        except Exception as e:
            logger.error(f"[SAM] Unexpected error for {soap_action}: {e}")
            return None

    def _parse_error(self, xml_text: str) -> tuple[Optional[str], Optional[str]]:
        try:
            root = ET.fromstring(xml_text)
            error_code_elems = root.findall(".//errorCode")
            if not error_code_elems:
                return "解析失败：无法找到错误码节点", None

            error_code = error_code_elems[0].text
            if error_code != "0":
                error_msg_elems = root.findall(".//errorMessage")
                error_msg = error_msg_elems[0].text if error_msg_elems else "未知错误"
                return f"接口返回错误 [代码: {error_code}]: {error_msg}", None

            return None, None
        except ET.ParseError as e:
            return f"XML 解析错误: {e}", None

    async def _extract_images_from_event(self, event: AstrMessageEvent) -> list[str]:
        images = []
        try:
            for msg in event.get_messages():
                if isinstance(msg, Image):
                    try:
                        base64_data = await msg.convert_to_base64()
                        images.append(f"base64://{base64_data}")
                    except Exception as e:
                        logger.error(f"[SAM] 提取图片base64失败: {e}")
        except Exception as e:
            logger.error(f"[SAM] 获取消息链失败: {e}")
        return images

    async def _call_ocr_model(self, image_base64: str) -> Optional[str]:
        try:
            logger.info(f"[SAM诊断] 调用OCR模型，图片长度: {len(image_base64)}")

            if image_base64.startswith("base64://"):
                image_base64 = image_base64[9:]
                logger.info(f"[SAM诊断] 已去除base64前缀，实际长度: {len(image_base64)}")

            url = "http://127.0.0.1:11434/api/chat"
            payload = {
                "model": "minicpm-v:latest",
                "messages": [
                    {
                        "role": "user",
                        "content": OCR_PROMPT,
                        "images": [image_base64]
                    }
                ],
                "stream": False
            }
            logger.info(f"[SAM诊断] 发送请求到Ollama...")

            import urllib.request
            import json as json_module

            req = urllib.request.Request(
                url,
                data=json_module.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=120) as response:
                data = json_module.loads(response.read().decode('utf-8'))
                content = data.get("message", {}).get("content", "")
                logger.info(f"[SAM诊断] Ollama返回内容长度: {len(content)}")
                if content:
                    return content.strip()

            logger.warning(f"[SAM诊断] OCR响应为空")
            return None

        except Exception as e:
            logger.error(f"[SAM诊断] OCR调用失败: {e}")
            import traceback
            logger.error(f"[SAM诊断] 详细错误: {traceback.format_exc()}")
            return None

    async def _generate_ai_response(self, ocr_result: str, user_description: str) -> str:
        try:
            clean_text = self._strip_html(ocr_result)
            category, matched_keyword = _match_problem_category(clean_text)

            system_prompt = """你是一个校园网故障诊断助手。请根据用户描述和图片OCR识别结果，结合知识库给出专业、简洁的诊断回复。

知识库内容：
- 连接故障：网线松动、网卡问题、IP配置错误、账号欠费、端口被禁用、私接路由器
- 服务器问题：目标网站宕机、维护、过载
- DNS问题：DNS解析失败、缓存损坏
- IP地址问题：IP冲突、DHCP冲突、MAC绑定失效
- 间歇性断网：线路接触不良、私接路由器性能不足、运营商链路问题
- Wi-Fi问题：连接私接Wi-Fi、信号弱、认证问题
- 网络中断：大面积中断、线路损坏
- 网速慢：带宽不足、私接路由器性能差、高峰期拥塞
- 打印机问题：网络不通、IP冲突、驱动问题
- 路由器问题：私接路由器导致MAC绑定冲突、DHCP冲突、广播风暴
- 证书问题：浏览器证书警告
- 账号问题：账号多设备在线、强制下线
- 套餐限制：免费套餐仅限办公区域使用

回复格式要求：
1. 先说明识别到的问题类型
2. 给出可能原因
3. 提供解决建议（操作步骤）
4. 回复结尾必须是：齐鲁工业大学网络运维（注意：不要把这句指令写进回复里，直接以"齐鲁工业大学网络运维"结尾即可）
5. 不要使用markdown格式，使用纯文本"""

            user_prompt = f"""图片OCR识别结果：
{clean_text}

用户描述：
{user_description}

请结合以上信息，给出诊断回复。"""

            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id="ollama/qwen2.5:7b",
                    prompt=f"{system_prompt}\n\n用户：{user_prompt}",
                    image_urls=[]
                ),
                timeout=60.0
            )

            if resp and resp.completion_text:
                return resp.completion_text.strip()

            return "抱歉，AI生成回复失败，请联系管理员处理。"

        except Exception as e:
            logger.error(f"[SAM诊断] AI生成回复失败: {e}")
            return f"抱歉，分析过程出错：{str(e)}"

    async def _generate_followup_response(self, previous_category: str, user_followup: str) -> str:
        try:
            system_prompt = f"""你是一个校园网故障诊断助手。用户之前咨询的是「{previous_category}」类型的问题，现在用户提出了追问。

重要规则：
1. 校园网禁止使用路由器，不要提及任何路由器相关操作
2. 涉及AP、交换机、线路等设备问题，告知用户联系网络运维处理（电话：89631358）
3. 只给出用户自己能操作的设备排查步骤（手机、电脑、浏览器等）

知识库内容：
- 连接故障：网线松动、网卡问题、IP配置错误、账号欠费、端口被禁用
- 服务器问题：目标网站宕机、维护、过载
- DNS问题：DNS解析失败、缓存损坏
- IP地址问题：IP冲突、DHCP冲突、MAC绑定失效
- 间歇性断网：线路接触不良、运营商链路问题
- Wi-Fi问题：信号弱、认证问题、连接官方Wi-Fi（QLU 5.8/QLU 2.4）
- 网络中断：大面积中断、线路损坏（请联系运维）
- 网速慢：带宽不足、高峰期拥塞
- 打印机问题：网络不通、IP冲突、驱动问题
- 证书问题：浏览器证书警告
- 账号问题：账号多设备在线、强制下线
- 套餐限制：免费套餐仅限办公区域使用

回复格式要求：
1. 先确认用户的问题
2. 只给出用户自己能操作的步骤
3. 需要设备维修的问题引导联系网络运维
4. 回复结尾必须是：齐鲁工业大学网络运维（注意：不要把这句指令写进回复里，直接以"齐鲁工业大学网络运维"结尾即可）
5. 不要使用markdown格式，使用纯文本"""

            user_prompt = f"""用户之前的咨询类型：{previous_category}
用户的追问：{user_followup}

请根据用户追问，给出详细的解答。"""

            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id="ollama/qwen2.5:7b",
                    prompt=f"{system_prompt}\n\n用户：{user_prompt}",
                    image_urls=[]
                ),
                timeout=120.0
            )

            if resp and resp.completion_text:
                return resp.completion_text.strip()

            return "抱歉，AI生成回复失败，请联系管理员处理。"

        except Exception as e:
            logger.error(f"[SAM诊断] 追问AI回复失败: {e}")
            return f"抱歉，分析过程出错：{str(e)}"

    async def _generate_text_response(self, user_message: str, previous_category: Optional[str] = None) -> Optional[str]:
        try:
            knowledge_base = """【网速慢/卡顿】
可能原因：带宽不足（同时使用人数过多或套餐带宽低）、高峰期网络拥塞、出口带宽不足、本地设备后台下载占用

解决方案：
1. 访问 speed.qlu.edu.cn 测速，对比套餐带宽是否达标
2. 检查是否有后台下载或更新正在占用带宽
3. 关闭不必要的后台联网程序
4. 避开高峰期使用
5. 如带宽确实不足，考虑升级套餐

【连接故障/无法上网】
可能原因：网线松动脱落、网卡故障或驱动问题、IP配置错误、账号欠费、端口被禁用

解决方案：
1. 检查网线是否牢固插入电脑网口和墙面面板
2. 登录校园网自助平台检查账号是否欠费
3. 运行ipconfig /release 释放IP，再运行 ipconfig /renew 重新获取
4. 确认网卡已启用，必要时更新网卡驱动
5. 如仍无法解决，联系网络运维 89631358

【Wi-Fi问题/无线连接】
可能原因：连接了私接路由器的Wi-Fi、信号强度不足、Wi-Fi信道干扰、Portal认证问题

解决方案：
1. 确认已连接校园网官方Wi-Fi（QLU 5.8或QLU 2.4），不是私接的Wi-Fi
2. 将设备移至更靠近无线接入点的位置
3. 连接Wi-Fi后打开浏览器访问任意网站触发Portal认证
4. 如使用Wi-Fi频繁出问题，建议使用网线直连

【DNS问题/域名解析失败】
可能原因：DNS服务器故障、本地DNS缓存损坏、无法到达DNS服务器

解决方案：
1. 打开网络连接属性，修改DNS为推荐设置
2. 以管理员身份运行命令提示符，执行 ipconfig /flushdns 清除DNS缓存
3. 运行 nslookup www.baidu.com 测试解析是否正常

【间歇性断网/不稳定】
可能原因：网线接触不良、信号干扰、设备过热、运营商链路不稳定

解决方案：
1. 重新插拔网线两端，更换网线测试
2. 检查墙面面板是否损坏
3. 如果问题持续，可能是线路或上游设备问题，联系网络运维 89631358

【IP冲突】
可能原因：两台设备设置了相同静态IP、DHCP地址池耗尽、MAC绑定失效

解决方案：
1. 确保IP获取方式为"自动获得IP地址"
2. 运行ipconfig /release 和 ipconfig /renew 重新获取IP
3. 如提示IP冲突，选择"是"强制释放
4. 联系网络运维重新绑定MAC

【账号问题/强制下线】
可能原因：账号在多台设备同时在线、账号被他人使用、异常登录

解决方案：
1. 登录校园网自助平台检查账号在线设备
2. 如有陌生设备，强制下线不认识的设备
3. 修改账号密码
4. 每次只用一台设备登录

【网络中断/大面积断网】
解决方案：
1. 确认是单台设备还是整栋楼断网
2. 询问周围同学网络状态
3. 大面积中断联系网络运维热线 89631358 报修
4. 使用手机4G/5G移动网络临时应急

【证书问题/连接不是专用连接】
解决方案：
1. 检查电脑日期时间是否正确
2. 尝试更换浏览器（如Chrome换Firefox）
3. 清除浏览器缓存和Cookie
4. 如仍不行，联系网络运维处理

【打印机问题】
可能原因：网络不通、打印机IP冲突、驱动问题、打印服务异常

解决方案：
1. 运行 ping 打印机IP地址 确认网络连通
2. 确认打印机已开机就绪，未处于脱机状态
3. 从控制面板删除原有打印机，重新搜索安装驱动
4. 按Win+R输入services.msc，重启Print Spooler服务"""

            if previous_category:
                system_prompt = f"""你是一个校园网故障诊断助手，但也可以友好地和用户聊天。用户之前咨询的是「{previous_category}」类型的问题，现在用户提出了追问。

重要规则：
1. 如果用户只是在闲聊、问候、表达情绪（如抱怨、吐槽），请像朋友一样友好地回应
2. 如果用户的问题确实与网络/技术故障相关，给出具体的解决建议
3. 校园网禁止使用路由器，不要提及任何路由器相关操作
4. 涉及AP、交换机、线路等设备问题，告知用户联系网络运维处理（电话：89631358）
5. 只给出用户自己能操作的设备排查步骤（手机、电脑、浏览器等）

知识库内容：
{knowledge_base}

回复格式要求：
1. 如果是闲聊或情绪表达，像朋友一样友好地回应
2. 如果是网络问题，先说明识别到的问题类型，再给出解决建议
3. 只给出用户自己能操作的步骤
4. 需要设备维修的问题引导联系网络运维
5. 回复结尾必须是：齐鲁工业大学网络运维（注意：不要把这句指令写进回复里，直接以"齐鲁工业大学网络运维"结尾即可）
6. 不要使用markdown格式，使用纯文本"""
                user_prompt = f"用户之前的咨询类型：{previous_category}\n用户的追问：{user_message}"
            else:
                system_prompt = f"""你是一个校园网故障诊断助手，但也可以友好地和用户聊天。

重要规则：
1. 如果用户只是在闲聊、问候、表达情绪（如抱怨、吐槽），请像朋友一样友好地回应，不要提及网络运维
2. 如果用户的消息明显是网络故障相关（如无法上网、网速慢、连不上Wi-Fi等），给出具体的解决建议
3. 校园网禁止使用路由器，不要提及任何路由器相关操作
4. 涉及AP、交换机、线路等设备问题，告知用户联系网络运维处理（电话：89631358）
5. 只给出用户自己能操作的设备排查步骤（手机、电脑、浏览器等）

知识库内容：
{knowledge_base}

回复格式要求：
1. 如果是闲聊或情绪表达，像朋友一样友好地回应，结尾可以是"齐鲁工业大学网络运维"
2. 如果是网络问题，先说明识别到的问题类型，再给出解决建议，结尾必须是"齐鲁工业大学网络运维"
3. 不要使用markdown格式，使用纯文本"""
                user_prompt = f"用户消息：{user_message}"

            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id="ollama/qwen2.5:7b",
                    prompt=f"{system_prompt}\n\n用户：{user_prompt}",
                    image_urls=[]
                ),
                timeout=120.0
            )

            if resp and resp.completion_text:
                return resp.completion_text.strip()

            return "抱歉，AI生成回复失败，请联系管理员处理。"

        except Exception as e:
            logger.error(f"[SAM诊断] 文字消息AI回复失败: {e}")
            return f"抱歉，分析过程出错：{str(e)}"

    async def _query_account_info(self, username: str) -> dict:
        soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <queryAccountProfiles>
      <accountId>{username}</accountId>
    </queryAccountProfiles>
  </soap:Body>
</soap:Envelope>
"""
        xml_text = await self._sam_request("http://api.spl.ruijie.com/queryAccountProfiles", soap_body)
        if not xml_text:
            return {"error": "连接错误：无法连接到 SAM 服务器"}

        err, _ = self._parse_error(xml_text)
        if err:
            return {"error": err}

        try:
            root = ET.fromstring(xml_text)
            result = {"account_id": username}

            user_name_elems = root.findall(".//userName")
            if user_name_elems and user_name_elems[0].text:
                result["user_name"] = user_name_elems[0].text.strip()

            real_name_elems = root.findall(".//realName")
            if real_name_elems and real_name_elems[0].text:
                result["real_name"] = real_name_elems[0].text.strip()

            status_elems = root.findall(".//accountState")
            if status_elems and status_elems[0].text:
                result["account_status"] = status_elems[0].text.strip()

            bind_num_elems = root.findall(".//bindNum")
            if bind_num_elems and bind_num_elems[0].text:
                result["bind_num"] = bind_num_elems[0].text.strip()

            return result
        except Exception as e:
            return {"error": f"解析错误: {e}"}

    async def _query_online_devices(self, username: str) -> dict:
        soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
   <SOAP-ENV:Body>
      <queryOnlineUserV2>
         <param>
            <limit>100</limit>
            <offSet>0</offSet>
            <userId>{username}</userId>
         </param>
      </queryOnlineUserV2>
   </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""
        xml_text = await self._sam_request("http://api.spl.ruijie.com/queryOnlineUserV2", soap_body)
        if not xml_text:
            return {"error": "连接错误：无法连接到 SAM 服务器", "devices": [], "count": 0}

        err, _ = self._parse_error(xml_text)
        if err:
            return {"error": err, "devices": [], "count": 0}

        try:
            root = ET.fromstring(xml_text)
            online_user_infos = root.findall(".//onlineUserInfosV2")

            if not online_user_infos:
                return {"devices": [], "count": 0}

            devices = []
            for info in online_user_infos:
                device = {
                    "account_id": info.find('userId').text if info.find('userId') is not None else 'N/A',
                    "mac_address": info.find('userMac').text if info.find('userMac') is not None else 'N/A',
                    "ip_address": info.find('userIpv4').text if info.find('userIpv4') is not None else 'N/A',
                    "device_type": info.find('terminalTypeDes').text if info.find('terminalTypeDes') is not None else 'N/A',
                    "online_time": info.find('onlineTime').text if info.find('onlineTime') is not None else 'N/A',
                    "area_name": info.find('areaName').text if info.find('areaName') is not None else 'N/A',
                    "service_id": info.find('serviceId').text if info.find('serviceId') is not None else 'N/A'
                }
                devices.append(device)

            return {"devices": devices, "count": len(devices)}
        except Exception as e:
            return {"error": f"解析错误: {e}", "devices": [], "count": 0}

    async def _query_login_logs(self, username: str) -> dict:
        from_login_time, to_login_time = self._get_query_days_range()
        from_logout_time, to_logout_time = self._get_query_days_range()

        soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <queryOnlineDetailV2>
      <queryOnlineDetailParams>
        <fromLoginTime>{from_login_time}</fromLoginTime>
        <fromLogoutTime>{from_logout_time}</fromLogoutTime>
        <limit>100</limit>
        <offSet>0</offSet>
        <toLoginTime>{to_login_time}</toLoginTime>
        <toLogoutTime>{to_logout_time}</toLogoutTime>
        <userId>{username}</userId>
      </queryOnlineDetailParams>
    </queryOnlineDetailV2>
  </soap:Body>
</soap:Envelope>
"""
        xml_text = await self._sam_request("http://api.spl.ruijie.com/queryOnlineDetailV2", soap_body)
        if not xml_text:
            return {"error": "连接错误：无法连接到 SAM 服务器", "logs": [], "total": 0}

        err, _ = self._parse_error(xml_text)
        if err:
            return {"error": err, "logs": [], "total": 0}

        try:
            root = ET.fromstring(xml_text)
            online_details = root.findall(".//onlineDetailInfo")
            total_elems = root.findall(".//total")
            total = total_elems[0].text if total_elems else "0"

            logs = []
            for detail in online_details:
                login_time = detail.find('loginTime').text if detail.find('loginTime') is not None else 'N/A'
                logout_time = detail.find('logoutTime').text if detail.find('logoutTime') is not None else 'N/A'

                if login_time and login_time != 'N/A':
                    login_time = login_time.replace('T', ' ').replace('+08:00', '').replace('.000+08:00', '')
                if logout_time and logout_time != 'N/A':
                    logout_time = logout_time.replace('T', ' ').replace('+08:00', '').replace('.000+08:00', '')

                online_sec = int(detail.find('onlineSec').text if detail.find('onlineSec') is not None else '0')
                hours, remainder = divmod(online_sec, 3600)
                minutes, seconds = divmod(remainder, 60)
                duration_str = f"{hours}小时{minutes}分{seconds}秒" if hours > 0 else f"{minutes}分{seconds}秒"

                log = {
                    "account_id": detail.find('userId').text if detail.find('userId') is not None else 'N/A',
                    "login_time": login_time,
                    "logout_time": logout_time,
                    "duration": duration_str,
                    "mac_address": detail.find('userMac').text if detail.find('userMac') is not None else 'N/A',
                    "device_type": detail.find('terminalTypeDes').text if detail.find('terminalTypeDes') is not None else 'N/A',
                    "area_name": detail.find('areaName').text if detail.find('areaName') is not None else 'N/A',
                    "service_id": detail.find('serviceId').text if detail.find('serviceId') is not None else 'N/A',
                    "terminate_cause": detail.find('terminateCause').text if detail.find('terminateCause') is not None else 'N/A'
                }
                logs.append(log)

            return {"logs": logs, "total": int(total) if str(total).isdigit() else 0}
        except Exception as e:
            return {"error": f"解析错误: {e}", "logs": [], "total": 0}

    async def _query_fail_logs(self, username: str) -> dict:
        from_date, to_date = self._get_query_days_range()

        soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <queryLoginFailLog>
      <queryLoginFailLogParams>
        <fromDate>{from_date}</fromDate>
        <limit>100</limit>
        <offSet>0</offSet>
        <toDate>{to_date}</toDate>
        <userId>{username}</userId>
      </queryLoginFailLogParams>
    </queryLoginFailLog>
  </soap:Body>
</soap:Envelope>
"""
        xml_text = await self._sam_request("http://api.spl.ruijie.com/queryLoginFailLog", soap_body)
        if not xml_text:
            return {"error": "连接错误：无法连接到 SAM 服务器", "logs": [], "total": 0}

        err, _ = self._parse_error(xml_text)
        if err:
            return {"error": err, "logs": [], "total": 0}

        try:
            root = ET.fromstring(xml_text)
            fail_logs = root.findall(".//loginFailLog")
            total_elems = root.findall(".//total")
            total = total_elems[0].text if total_elems else "0"

            logs = []
            for log in fail_logs:
                create_time = log.find('createTime').text if log.find('createTime') is not None else 'N/A'
                if create_time and create_time != 'N/A':
                    create_time = create_time.replace('T', ' ').replace('+08:00', '')

                msg = log.find('msg').text if log.find('msg') is not None else 'N/A'

                user_id = 'N/A'
                area = 'N/A'
                service = 'N/A'
                access_type = 'N/A'
                user_mac = 'N/A'
                location = 'N/A'
                reason = 'N/A'

                if msg and msg != 'N/A':
                    try:
                        for part in msg.split(', '):
                            if ':' in part:
                                key_value = part.split(':', 1)
                                if len(key_value) == 2:
                                    key = key_value[0].strip()
                                    value = key_value[1].strip()
                                    if key == '用户':
                                        user_id = value.strip('()')
                                    elif key == '地区':
                                        area = value
                                    elif key == '服务':
                                        service = value
                                    elif key == '接入方式':
                                        access_type = value
                                    elif key == 'MAC':
                                        user_mac = value
                                    elif key == '接入位置描述':
                                        location = value
                                    elif key == '原因':
                                        reason = value
                    except Exception:
                        pass

                logs.append({
                    "account_id": user_id,
                    "fail_time": create_time,
                    "mac_address": user_mac,
                    "access_type": access_type,
                    "area": area,
                    "service": service,
                    "location": location,
                    "reason": reason
                })

            return {"logs": logs, "total": int(total) if str(total).isdigit() else 0}
        except Exception as e:
            return {"error": f"解析错误: {e}", "logs": [], "total": 0}

    def _filter_sensitive_info(self, data: dict) -> dict:
        return {k: v for k, v in data.items() if k not in ["ip_address", "nas_ip", "nas_port"]}

    def _filter_devices(self, devices: list) -> list:
        return [self._filter_sensitive_info(d) for d in devices]

    def _filter_logs(self, logs: list) -> list:
        return [self._filter_sensitive_info(l) for l in logs]

    def _strip_markdown(self, text: str) -> str:
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        text = re.sub(r'__(.+?)__', r'\1', text)
        text = re.sub(r'_(.+?)_', r'\1', text)
        text = re.sub(r'#+\s+', '', text)
        text = re.sub(r'^-+\s*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'^[-*]\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'`(.+?)`', r'\1', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _generate_diagnostic_prompt(self, net_data: dict, kb_info: str, ocr_info: str = "") -> str:
        ocr_context = f"\n【图片识别信息】\n{ocr_info}" if ocr_info else ""
        return f"""你是齐鲁工业大学校园网客服。请根据以下真实数据诊断网络问题，禁止编造数据。

【查询数据】
- 学号: {net_data.get('student_id')}
- 账号状态: {net_data.get('status_desc')}
- 最后报错: {net_data.get('last_login_error')}
{ocr_context}

{kb_info}

【要求】
1. 基于数据给出诊断结论
2. 提供1-2步具体解决方法
3. 回复限80字内
4. 结尾必须包含"齐鲁工业大学网络运维"

输出诊断回复："""

    async def _call_llm_api(self, system_prompt: str, user_message: str, account_id: str = "", timeout: float = 60.0) -> Optional[str]:
        try:
            full_prompt = f"{system_prompt}\n\n{user_message}"
            if "{{account_id}}" in full_prompt and account_id:
                full_prompt = full_prompt.replace("{{account_id}}", account_id)

            provider_id = self.config.get("provider_settings", {}).get("default_provider_id", "")
            if not provider_id:
                provider_id = self.config.get("llm_provider_id", "")

            logger.info(f"[SAM诊断] 调用LLM, provider_id={provider_id}, prompt长度={len(full_prompt)}")

            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=full_prompt
                ),
                timeout=timeout
            )
            if resp and resp.completion_text:
                logger.info(f"[SAM诊断] LLM响应成功，长度={len(resp.completion_text)}")
                return resp.completion_text.strip()
            logger.warning(f"[SAM诊断] LLM响应为空")
            return None
        except asyncio.TimeoutError:
            logger.warning(f"[SAM诊断] LLM调用超时（{timeout}秒）")
            return None
        except Exception as e:
            logger.error(f"[SAM诊断] LLM调用失败: {e}")
            return None

    def _extract_error_info_from_ocr(self, ocr_context: str) -> str:
        if not ocr_context:
            return "未能识别截图中的错误信息，请确认学号是否正确。"
        clean_text = self._strip_html(ocr_context)
        if len(clean_text) > 200:
            clean_text = clean_text[:200] + "..."
        return clean_text

    def _strip_html(self, text: str) -> str:
        import re
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _clear_user_state(self, user_id: str):
        self.pending_users.discard(user_id)
        self.user_selected_type.pop(user_id, None)
        self.pending_verification.pop(user_id, None)
        self.pending_reply.pop(user_id, None)
        self.pending_login_log.pop(user_id, None)
        self.pending_fail_log.pop(user_id, None)
        self.diagnosis_context.pop(user_id, None)
        self.pending_user_type_selection.discard(user_id)
        self.pending_diagnosis.pop(user_id, None)

    @filter.event_message_type(EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        message_str = event.message_str or ""
        user_id = event.get_sender_id()
        msg_type = get_message_type_str(event)

        if msg_type == "GroupMessage":
            is_mentioned = getattr(event, 'is_at_or_wake_command', False)
            if is_mentioned:
                yield event.plain_result(GROUP_GUIDE_TEXT)
                event.stop_event()
            return

        if msg_type == "FriendMessage":
            images = await self._extract_images_from_event(event)
            msg = message_str.strip()

            if images:
                self.pending_hint.discard(user_id)
                yield event.plain_result(IMAGE_PROCESSING_TEXT)
                event.stop_event()

                ocr_result = await self._call_ocr_model(images[0])
                if not ocr_result:
                    yield event.plain_result("图片识别失败，请重试或换一张更清晰的图片。")
                    return

                self.last_image_time[user_id] = time.time()
                if msg:
                    yield event.plain_result("正在分析，请稍候...")
                    response = await self._generate_ai_response(ocr_result, msg)
                    yield event.plain_result(response)
                else:
                    self.pending_image_ocr[user_id] = ocr_result
                    yield event.plain_result("图片已识别成功。请描述一下您的具体问题或症状，我将结合图片内容为您分析。")
                return

            if user_id in self.pending_image_ocr:
                ocr_result = self.pending_image_ocr.pop(user_id)
                yield event.plain_result("正在分析，请稍候...")
                event.stop_event()
                response = await self._generate_ai_response(ocr_result, msg)
                if response:
                    yield event.plain_result(response)
                return

            last_img_time = self.last_image_time.get(user_id, 0)
            if time.time() - last_img_time < 60:
                ocr_result = self.pending_image_ocr.get(user_id)
                if ocr_result:
                    self.pending_image_ocr.pop(user_id, None)
                    self.last_image_time.pop(user_id, None)
                    yield event.plain_result("正在分析，请稍候...")
                    response = await self._generate_ai_response(ocr_result, msg)
                    if response:
                        yield event.plain_result(response)
                    return

            if user_id in self.diagnosis_context:
                async for ret in self._handle_diagnosis_verification(event, msg):
                    yield ret
                event.stop_event()
                return

            if user_id in self.pending_verification:
                async for ret in self._handle_name_verification(event, msg):
                    yield ret
                event.stop_event()
                return

            if user_id not in self.pending_hint:
                self.pending_hint.add(user_id)

            if msg:
                self.last_user_message[user_id] = msg
                last_cat = self.last_category.get(user_id)
                yield event.plain_result("正在分析您的问题，请稍候...")
                response = await self._generate_text_response(msg, last_cat)
                if response:
                    yield event.plain_result(response)
                event.stop_event()
            return

    async def _handle_name_verification(self, event: AstrMessageEvent, user_input: str):
        user_id = event.get_sender_id()
        if user_id not in self.pending_verification:
            return

        input_name = user_input.strip()
        if not input_name:
            return

        ctx = self.pending_verification[user_id]
        expected_name = ctx["user_name"]
        retry_count = ctx.get("retry_count", 3)
        query_type = ctx.get("query_type", "device")

        if input_name == expected_name:
            if query_type in ["login_log", "fail_log"]:
                result = ctx.get("result", "")
                yield event.plain_result(result)
            else:
                result = self.pending_reply.get(user_id, "")
                await event.bot.send_private_msg(user_id=int(user_id), message=result)

            self._clear_user_state(user_id)
            logger.info(f"用户 [{user_id}] 姓名验证成功")
        else:
            retry_count -= 1
            if retry_count <= 0:
                self._clear_user_state(user_id)
                account_type = ctx.get("account_type", "账号")
                yield event.plain_result(f"{account_type}姓名验证失败次数过多。请稍后再试。")
                logger.info(f"用户 [{user_id}] 姓名验证失败，已达最大重试次数")
            else:
                self.pending_verification[user_id]["retry_count"] = retry_count
                account_type = ctx.get("account_type", "账号")
                yield event.plain_result(f"姓名不匹配，请重新输入（剩余 {retry_count} 次尝试机会）:\n请输入该{account_type}登记的姓名:")

    async def _handle_diagnosis_verification(self, event: AstrMessageEvent, user_input: str):
        user_id = event.get_sender_id()
        input_name = user_input.strip()

        if not input_name or user_id not in self.diagnosis_context:
            return

        ctx = self.diagnosis_context[user_id]
        expected_name = ctx.get("user_name", "")
        retry_count = ctx.get("retry_count", 3)

        if input_name == expected_name:
            llm_analysis = ctx.get("llm_analysis", "")
            llm_analysis = self._strip_markdown(llm_analysis)

            final_result = f"📋 诊断结果\n{'='*40}\n"
            final_result += f"账号：{ctx.get('account_id', 'N/A')}\n"
            final_result += f"姓名：{expected_name}\n"
            final_result += f"{'-'*40}\n"
            final_result += f"{llm_analysis}\n"
            final_result += f"{'-'*40}\n齐鲁工业大学网络运维"

            await event.bot.send_private_msg(user_id=int(user_id), message=final_result)
            self._clear_user_state(user_id)
            logger.info(f"用户 [{user_id}] 网络诊断验证成功")
        else:
            retry_count -= 1
            if retry_count <= 0:
                self._clear_user_state(user_id)
                yield event.plain_result("姓名验证失败次数过多，网络诊断已取消。请稍后再试。")
                logger.info(f"用户 [{user_id}] 网络诊断姓名验证失败")
            else:
                self.diagnosis_context[user_id]["retry_count"] = retry_count
                yield event.plain_result(f"姓名不匹配，请重新输入（剩余 {retry_count} 次尝试机会）:\n请输入该账号登记的姓名:")

    async def process_image_diagnostic(self, event: AstrMessageEvent, images: list[str]):
        user_id = event.get_sender_id()

        try:
            ocr_result = await self._call_ocr_model(images[0])
            if not ocr_result:
                await event.bot.send_private_msg(user_id=int(user_id), message="图片识别失败，请重试或换一张更清晰的图片。")
                return

            logger.info(f"[SAM诊断] OCR结果: {ocr_result[:200]}...")

            clean_text = self._strip_html(ocr_result)
            category, matched_keyword = _match_problem_category(clean_text)

            import re
            student_id_match = re.search(r'\b(202\d{10}|5\d{10})\b', clean_text)
            student_id = student_id_match.group(1) if student_id_match else None

            response = self._generate_knowledge_response(category, clean_text, student_id)

            await event.bot.send_private_msg(user_id=int(user_id), message=response)

        except Exception as e:
            logger.error(f"[SAM诊断] 图片诊断异常: {e}")
            try:
                await event.bot.send_private_msg(user_id=int(user_id), message=f"诊断过程出错：{str(e)}")
            except Exception:
                pass

    def _generate_knowledge_response(self, category: str, ocr_text: str, student_id: str = None) -> str:
        sep = "=" * 40

        if category == "连接故障":
            if "超时" in ocr_text or "认证" in ocr_text:
                msg = f"""您好！根据您发送的图片，认证服务器响应超时。
{sep}
问题说明：
认证服务器暂时未响应您的请求，这通常是校园网设备负载过高导致的。

解决方法：
请等待一段时间（5-10分钟）后再重新连接，通常可以恢复正常。

如果等待后仍无法连接：
1. 检查网线是否连接正常
2. 尝试重新认证
3. 如持续无法连接，请在"鹏云校园"微信公众号报修

{sep}
齐鲁工业大学网络运维"""
            else:
                msg = f"""您好！根据您发送的图片，您遇到的是网络连接故障。
{sep}
可能原因：
1. 网线松动或脱落
2. 网卡故障或驱动问题
3. IP配置错误
4. 校园网账号问题（欠费、未绑定MAC）
5. 端口被禁用

建议操作：
1. 检查网线是否牢固插入
2. 运行 `ipconfig /release` 然后 `ipconfig /renew`
3. 登录 swrz.qlu.edu.cn 检查账号状态
4. 确认是否使用私接路由器（校园网禁止使用）

如仍无法解决，请在"鹏云校园"微信公众号报修。
{sep}
齐鲁工业大学网络运维"""
        elif category == "服务器问题":
            msg = f"""您好！根据您发送的图片，您遇到的是服务器不可用问题。
{sep}
可能原因：
1. 目标网站服务器宕机或维护中
2. 服务器过载
3. 网络路径故障

建议操作：
1. 等待几分钟后重试
2. 访问其他网站确认是否全网问题
3. 清除浏览器缓存后重试

{sep}
齐鲁工业大学网络运维"""
        elif category == "DNS问题":
            msg = f"""您好！根据您发送的图片，您遇到的是DNS解析问题。
{sep}
建议操作：
1. 运行 `ipconfig /flushdns` 清除DNS缓存
2. 检查网络连接属性中的DNS设置
3. 推荐使用DNS：223.5.5.5
4. 尝试使用无痕模式访问

注意：如果您使用了私接路由器，请移除。

{sep}
齐鲁工业大学网络运维"""
        elif category == "IP地址问题":
            msg = f"""您好！根据您发送的图片，您遇到的是IP地址冲突或未分配问题。
{sep}
可能原因：
1. 两台设备设置了相同静态IP
2. 私接路由器导致DHCP冲突
3. MAC地址绑定失效

建议操作：
1. 运行 `ipconfig /release` 然后 `ipconfig /renew`
2. 确保使用DHCP自动获取IP
3. 移除私接路由器，直连接入
4. 如更换了网卡，请登录 swrz.qlu.edu.cn 重新绑定MAC

{sep}
齐鲁工业大学网络运维"""
        elif category == "间歇性断网":
            msg = f"""您好！根据您发送的图片，您的网络存在间歇性断网问题。
{sep}
可能原因：
1. 网线接触不良
2. 私接路由器性能不足
3. 运营商链路不稳定

建议操作：
1. 检查网线连接，重新插拔
2. 移除私接路由器，直连接入测试
3. 更换网线排除线缆故障
4. 如问题持续，拨打运维热线 89631358

{sep}
齐鲁工业大学网络运维"""
        elif category == "Wi-Fi问题":
            msg = f"""您好！根据您发送的图片，您遇到的是无线网络连接问题。
{sep}
建议操作：
1. 请连接校园网官方Wi-Fi（QLU 5.8或QLU 2.4）
2. 不要连接私接路由器的Wi-Fi
3. 连接后打开浏览器访问任意网站触发Portal认证
4. 如信号弱，请移至更靠近接入点的位置

{sep}
齐鲁工业大学网络运维"""
        elif category == "网络中断":
            msg = f"""您好！根据您发送的图片，您遇到的是网络中断问题。
{sep}
建议操作：
1. 确认是单台设备还是整栋楼断网
2. 检查周边同学网络状态
3. 检查AP指示灯和设备通电状态

大面积中断请拨打运维热线 89631358 报修

{sep}
齐鲁工业大学网络运维"""
        elif category == "网速慢":
            msg = f"""您好！根据您发送的图片，您遇到的是网速慢/延迟高问题。
{sep}
建议操作：
1. 访问 speed.qlu.edu.cn 测速，对比套餐带宽
2. 移除私接路由器，直连接入测速
3. 检查是否有后台下载占用带宽
4. 关闭不必要的后台联网程序
5. 避开高峰期使用

{sep}
齐鲁工业大学网络运维"""
        elif category == "打印机问题":
            msg = f"""您好！根据您发送的图片，您遇到的是网络打印机问题。
{sep}
建议操作：
1. 确认电脑和打印机在同一网络
2. 运行 `ping 打印机IP` 测试连通性
3. 确认打印机已开机且未处于脱机状态
4. 从控制面板删除并重新添加打印机
5. 按Win+R输入services.msc，重启Print Spooler服务

{sep}
齐鲁工业大学网络运维"""
        elif category == "路由器问题":
            msg = f"""您好！检测到您可能使用了私接路由器，这会导致多种网络问题。
{sep}
校园网规定：禁止使用私接路由器！

私接路由器的危害：
1. MAC地址绑定冲突 - 部分设备无法上网
2. DHCP冲突 - 导致IP分配混乱
3. 广播风暴 - 可能导致整栋楼网络瘫痪
4. 绕开认证 - 导致账号被封禁

正确的上网方式：
1. 电脑直连墙面端口，通过DHCP获取IP
2. 多设备上网请使用交换机（非路由器）
3. 或连接校园网官方Wi-Fi（QLU 5.8/QLU 2.4）

如账号已被封禁，请联系网络运维解封。

{sep}
齐鲁工业大学网络运维"""
        elif category == "证书问题":
            msg = f"""您好！根据您发送的图片，您遇到的是SSL证书安全警告问题。
{sep}
问题说明：
当访问校园网认证页面(swrz.qlu.edu.cn)时出现证书警告，这是因为浏览器检测到SSL证书可能存在问题。

建议操作：
1. 点击浏览器页面中的"高级"选项
2. 选择"继续前往 swrz.qlu.edu.cn（不安全）"
3. 如果无法正常跳转认证页面，尝试：
   - 使用Chrome内核浏览器访问
   - 清除浏览器缓存后重试
   - 检查系统时间是否正确

临时解决方案：
直接访问认证地址：https://swrz.qlu.edu.cn

如仍无法解决，请在"鹏云校园"微信公众号报修。
{sep}
齐鲁工业大学网络运维"""
        elif category == "账号问题":
            msg = f"""您好！根据您发送的图片，您的账号提示运营商账号已在线，需要强制下线。
{sep}
问题说明：
您的账号在其他设备上已登录，导致当前设备无法正常使用校园网。

解决方法：
请前往校园网自助服务平台（swzz.qlu.pwnyun.com）手动强制下线该账号，然后重新连接即可。

操作步骤：
1. 访问 swzz.qlu.pwnyun.com
2. 登录您的账号
3. 找到"强制下线"或"账号管理"选项
4. 下线该账号后，重新连接校园网

{sep}
齐鲁工业大学网络运维"""
        elif category == "套餐咨询":
            msg = f"""您好！根据您发送的图片，这是关于校园网套餐限制的说明。
{sep}
套餐限制说明：
免费套餐（研究生/教工）：仅限长清校区办公区域有线连接使用

如您有其他用网需求，请开通付费校园网套餐，详情请咨询校园网运维中心。

联系方式：
- 运维热线：89631358
- 故障报修："鹏云校园"微信公众号
{sep}
齐鲁工业大学网络运维"""
        else:
            if student_id:
                msg = f"""您好！我已识别到您的学号信息。
{sep}
根据您发送的图片内容，我暂时无法确定具体的故障类型。
建议您：

1. 检查网络连接是否正常
2. 尝试打开浏览器访问任意网站
3. 运行 `ipconfig /renew` 获取IP地址

如仍无法解决，请在"鹏云校园"微信公众号报修。
{sep}
齐鲁工业大学网络运维"""
            else:
                msg = f"""您好！根据您发送的图片，我无法确定具体的网络故障类型。
{sep}
建议您联系群内管理员处理，或上报工单。

故障报修："鹏云校园"微信公众号
运维热线：89631358
{sep}
齐鲁工业大学网络运维"""

        return msg

    async def _quick_diagnosis(self, student_id: str, event: AstrMessageEvent, ocr_context: str = ""):
        user_id = event.get_sender_id()

        is_valid, account_type = self.validate_account_format(student_id)
        if not is_valid:
            logger.info(f"[SAM诊断] 学号格式不完全匹配但仍尝试查询: {student_id}")
            account_type = "账号"

        logger.info(f"[SAM诊断] 开始快速诊断账号 [{student_id}]")

        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    self._query_account_info(student_id),
                    self._query_online_devices(student_id),
                    self._query_login_logs(student_id),
                    self._query_fail_logs(student_id),
                    return_exceptions=True
                ),
                timeout=20.0
            )

            account_info, online_devices, login_logs, fail_logs = results
            names = ["账户信息", "在线设备", "登录日志", "失败日志"]
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    results[i] = {"error": f"{names[i]}查询失败: {str(r)}"}
            account_info, online_devices, login_logs, fail_logs = results
            logger.info(f"[SAM诊断] 查询完成: account={account_info.get('user_name', 'N/A')}, devices={online_devices.get('count', 0)}, logs={login_logs.get('total', 0)}, fails={fail_logs.get('total', 0)}")
        except asyncio.TimeoutError:
            logger.error(f"[SAM诊断] 账号 [{student_id}] 诊断超时（20秒）")
            await event.bot.send_private_msg(user_id=int(user_id), message="诊断超时，SAM 服务器响应过慢，请稍后再试。")
            return
        except Exception as e:
            logger.error(f"[SAM诊断] 查询阶段异常: {e}")
            await event.bot.send_private_msg(user_id=int(user_id), message=f"诊断过程出错：{str(e)}")
            return

        has_error = any("error" in result for result in [account_info, online_devices, login_logs, fail_logs])
        account_not_exist = "30001" in str(account_info.get("error", ""))

        if account_not_exist:
            error_info = self._extract_error_info_from_ocr(ocr_context)
            result_msg = f"📋 诊断结果\n{'='*40}\n"
            result_msg += f"账号：{student_id}\n"
            result_msg += f"{'-'*40}\n"
            result_msg += f"❌ 账户不存在或学号识别有误\n"
            result_msg += f"\n根据截图信息诊断：\n{error_info}\n"
            result_msg += f"{'-'*40}\n齐鲁工业大学网络运维"
            await event.bot.send_private_msg(user_id=int(user_id), message=result_msg)
            return

        if has_error:
            error_msg = "查询过程中遇到错误，无法完成诊断"
            for name, result in [("账户信息", account_info), ("在线设备", online_devices),
                                ("登录日志", login_logs), ("失败日志", fail_logs)]:
                if "error" in result:
                    error_msg = f"查询{name}时出错：{result['error']}"
                    break
            await event.bot.send_private_msg(user_id=int(user_id), message=error_msg)
            return

        user_name = account_info.get("user_name") or account_info.get("real_name") or ""
        account_status = account_info.get("account_status", "未知")
        bind_num = int(account_info.get("bind_num", "0") or "0")

        devices = self._filter_devices(online_devices.get("devices", []))
        logs = self._filter_logs(login_logs.get("logs", []))
        fail_entries = self._filter_logs(fail_logs.get("logs", []))

        net_data = {
            "student_id": student_id,
            "status_desc": account_status,
            "last_login_error": fail_entries[0].get("reason", "无") if fail_entries else "无"
        }
        kb_info = f"用户{user_name}，账号状态{account_status}，绑定设备数{bind_num}，在线设备{len(devices)}台，登录日志{login_logs.get('total',0)}条，失败日志{fail_logs.get('total',0)}条"
        analysis_prompt = self._generate_diagnostic_prompt(net_data, kb_info, ocr_context)

        llm_result = None
        try:
            if self._get_config().get("enable_llm_analysis", True):
                logger.info(f"[SAM诊断] 开始调用LLM (timeout=60s)...")
                llm_result = await self._call_llm_api(
                    self._get_config().get("llm_system_prompt", ""),
                    analysis_prompt,
                    student_id
                )

            if llm_result:
                llm_result = self._strip_markdown(llm_result)
                final_result = f"📋 诊断结果\n{'='*40}\n"
                final_result += f"账号：{student_id}\n"
                final_result += f"姓名：{user_name or '未找到'}\n"
                final_result += f"{'-'*40}\n"
                final_result += f"{llm_result}\n"
                final_result += f"{'-'*40}\n齐鲁工业大学网络运维"

                await event.bot.send_private_msg(user_id=int(user_id), message=final_result)
                logger.info(f"用户 [{user_id}] 图片诊断成功")
            else:
                result_msg = self._format_diagnosis_fallback(
                    student_id, account_type, user_name,
                    account_status, bind_num,
                    online_devices, login_logs, fail_logs,
                    devices, logs, fail_entries
                )
                await event.bot.send_private_msg(user_id=int(user_id), message=result_msg)
        except Exception as e:
            logger.error(f"[SAM诊断] LLM/结果阶段异常: {e}")
            try:
                await event.bot.send_private_msg(user_id=int(user_id), message="诊断分析出错，请稍后再试。")
            except Exception:
                pass

    def _format_diagnosis_fallback(self, username: str, account_type: str, user_name: str,
                                   account_status: str, bind_num: int,
                                   online_devices: dict, login_logs: dict, fail_logs: dict,
                                   devices: list, logs: list, fail_entries: list) -> str:
        result = f"📋 诊断结果\n{'='*40}\n"
        result += f"账号：{username}\n"
        result += f"姓名：{user_name or '未找到'}\n"
        result += f"账号类型：{account_type}\n"
        result += f"账号状态：{account_status}\n"
        result += f"绑定设备数：{bind_num}\n"
        result += f"{'-'*40}\n"

        result += f"📱 在线设备（共{online_devices.get('count', 0)}台）\n"
        if devices:
            for i, dev in enumerate(devices[:3], 1):
                result += f"  {i}. {dev.get('device_type','N/A')}，{dev.get('area_name','N/A')}\n"
        else:
            result += "  无在线设备\n"

        result += f"📋 登录日志（{login_logs.get('total', 0)}条）\n"
        result += f"❌ 失败日志（{fail_logs.get('total', 0)}条）\n"
        result += f"{'-'*40}\n齐鲁工业大学网络运维"
        return result

    async def process_campus_net_diagnostic(self, student_id: str, event: AstrMessageEvent):
        user_id = event.get_sender_id()

        try:
            await event.bot.send_private_msg(user_id=int(user_id), message="正在为您进行网络问题诊断，请稍候...")
        except Exception as e:
            logger.error(f"[SAM诊断] 发送初始消息失败: {e}")
            return

        is_valid, account_type = self.validate_account_format(student_id)
        if not is_valid:
            try:
                await event.bot.send_private_msg(user_id=int(user_id), message="账号格式错误，无法进行诊断")
            except Exception:
                pass
            return

        logger.info(f"[SAM诊断] 开始诊断账号 [{student_id}]")

        try:
            logger.info(f"[SAM诊断] 开始并行查询4项数据...")
            results = await asyncio.wait_for(
                asyncio.gather(
                    self._query_account_info(student_id),
                    self._query_online_devices(student_id),
                    self._query_login_logs(student_id),
                    self._query_fail_logs(student_id),
                    return_exceptions=True
                ),
                timeout=20.0
            )

            account_info, online_devices, login_logs, fail_logs = results
            names = ["账户信息", "在线设备", "登录日志", "失败日志"]
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    results[i] = {"error": f"{names[i]}查询失败: {str(r)}"}
            account_info, online_devices, login_logs, fail_logs = results
            logger.info(f"[SAM诊断] 查询完成: account={account_info.get('user_name', 'N/A')}, devices={online_devices.get('count', 0)}, logs={login_logs.get('total', 0)}, fails={fail_logs.get('total', 0)}")
        except asyncio.TimeoutError:
            logger.error(f"[SAM诊断] 账号 [{student_id}] 诊断超时（20秒）")
            try:
                await event.bot.send_private_msg(user_id=int(user_id), message="诊断超时，SAM 服务器响应过慢，请稍后再试。")
            except Exception:
                pass
            return
        except Exception as e:
            logger.error(f"[SAM诊断] 查询阶段异常: {e}")
            try:
                await event.bot.send_private_msg(user_id=int(user_id), message=f"诊断过程出错：{str(e)}")
            except Exception:
                pass
            return

        has_error = any("error" in result for result in [account_info, online_devices, login_logs, fail_logs])

        if has_error:
            error_msg = "查询过程中遇到错误，无法完成诊断"
            for name, result in [("账户信息", account_info), ("在线设备", online_devices),
                                ("登录日志", login_logs), ("失败日志", fail_logs)]:
                if "error" in result:
                    error_msg = f"查询{name}时出错：{result['error']}"
                    break
            await event.bot.send_private_msg(user_id=int(user_id), message=error_msg)
            return

        user_name = account_info.get("user_name") or account_info.get("real_name") or ""
        account_status = account_info.get("account_status", "未知")
        bind_num = int(account_info.get("bind_num", "0") or "0")

        devices = self._filter_devices(online_devices.get("devices", []))
        logs = self._filter_logs(login_logs.get("logs", []))
        fail_entries = self._filter_logs(fail_logs.get("logs", []))

        net_data = {
            "student_id": student_id,
            "status_desc": account_status,
            "last_login_error": fail_entries[0].get("reason", "无") if fail_entries else "无"
        }
        kb_info = f"用户{user_name}，账号状态{account_status}，绑定设备数{bind_num}，在线设备{len(devices)}台，登录日志{login_logs.get('total',0)}条，失败日志{fail_logs.get('total',0)}条"
        analysis_prompt = self._generate_diagnostic_prompt(net_data, kb_info)

        llm_result = None
        try:
            if self._get_config().get("enable_llm_analysis", True):
                logger.info(f"[SAM诊断] 开始调用LLM (timeout=60s)...")
                llm_result = await self._call_llm_api(
                    self._get_config().get("llm_system_prompt", ""),
                    analysis_prompt,
                    student_id
                )
                if llm_result:
                    logger.info(f"[SAM诊断] LLM返回成功，长度={len(llm_result)}")

            if llm_result:
                self.diagnosis_context[user_id] = {
                    "account_id": student_id,
                    "account_type": account_type,
                    "user_name": user_name,
                    "llm_analysis": llm_result,
                    "retry_count": 3
                }
                try:
                    await event.bot.send_private_msg(user_id=int(user_id), message="请输入该账号登记的姓名进行验证:")
                except Exception as e:
                    logger.error(f"[SAM诊断] 发送验证请求失败: {e}")
            else:
                logger.info(f"[SAM诊断] 使用fallback模式展示数据")
                result_msg = self._format_diagnosis_fallback(
                    student_id, account_type, user_name,
                    account_status, bind_num,
                    online_devices, login_logs, fail_logs,
                    devices, logs, fail_entries
                )
                try:
                    await event.bot.send_private_msg(user_id=int(user_id), message=result_msg)
                except Exception as e:
                    logger.error(f"[SAM诊断] 发送fallback结果失败: {e}")
        except Exception as e:
            logger.error(f"[SAM诊断] LLM/结果阶段异常: {e}")
            try:
                await event.bot.send_private_msg(user_id=int(user_id), message="诊断分析出错，请稍后再试。")
            except Exception:
                pass

    async def terminate(self):
        self.user_query_times.clear()
        self.pending_verification.clear()
        self.pending_reply.clear()
        self.diagnosis_context.clear()
        self.pending_users.clear()
        self.user_selected_type.clear()
        self.pending_user_type_selection.clear()
        self.pending_login_log.clear()
        self.pending_fail_log.clear()
        self.pending_diagnosis.clear()
        self.pending_hint.clear()
