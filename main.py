from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star
from astrbot.api import logger
import aiohttp
import base64
import xml.etree.ElementTree as ET
import re
import time
from datetime import datetime, timedelta
import pytz

class UserDevicesPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = context.get_config()
        self.pending_users = set()
        self.user_query_times = {}
        self.pending_verification = {}
        self.pending_reply = {}
        self.pending_user_type_selection = set()
        self.user_selected_type = {}
        self.pending_login_log = {}
        self.pending_fail_log = {}
        
    def _is_trigger(self, message: str) -> bool:
        if not self._is_feature_enabled("device"):
            return False
        keywords = ["在线设备", "查询设备", "设备查询", "在线用户", "查询用户", "用户查询", "zscx", "设备", "用户", "在线", "查询", "cx", "sb", "yh"]
        return any(kw in message for kw in keywords)
    
    def _is_login_log_trigger(self, message: str) -> bool:
        if not self._is_feature_enabled("login_log"):
            return False
        keywords = ["上线日志", "登录日志"]
        return any(kw in message for kw in keywords)
    
    def _is_fail_log_trigger(self, message: str) -> bool:
        if not self._is_feature_enabled("fail_log"):
            return False
        keywords = ["失败日志", "登录失败", "登录异常"]
        return any(kw in message for kw in keywords)
    
    def _extract_id_from_query(self, message: str) -> str:
        cleaned = re.sub(r'^@\S+\s*', '', message).strip()
        
        patterns = [
            r'^设备查询\s+(1043\d{7})',
            r'^设备查询\s+(5\d{5})',
            r'^设备查询\s+(H[A-Za-z0-9]{6})',
            r'^设备查询\s+(202[4-9]\d{8})'
        ]
        
        for pattern in patterns:
            match = re.match(pattern, cleaned)
            if match:
                return match.group(1)
        
        return ""
    
    async def _process_query(self, event: AstrMessageEvent, account_id: str):
        user_id = event.get_sender_id()
        
        is_valid, account_type = self.validate_account_format(account_id)
        if not is_valid:
            await event.bot.send_private_msg(
                user_id=int(user_id),
                message=self.get_error_message_for_invalid_format(account_id)
            )
            return
        
        remaining = self._check_rate_limit(user_id)
        if remaining > 0:
            await event.bot.send_private_msg(
                user_id=int(user_id),
                message=f"查询过于频繁，请 {remaining} 秒后再试"
            )
            return
        
        logger.info(f"查询{account_type} [{account_id}] 的在线设备")
        status, user_name, result = await self.query_devices(account_id)
        
        if status == "error":
            await event.bot.send_private_msg(user_id=int(user_id), message=result)
            return
        
        if status == "offline":
            await event.bot.send_private_msg(
                user_id=int(user_id),
                message=f"用户 {account_id} 无设备在线"
            )
            return
        
        if status == "online":
            self.pending_verification[user_id] = {
                "account_id": account_id,
                "account_type": account_type,
                "user_name": user_name,
                "retry_count": 3
            }
            self.pending_reply[user_id] = result
            
            await event.bot.send_private_msg(
                user_id=int(user_id),
                message=f"请输入该{account_type}登记的姓名进行验证:"
            )
            return
    
    def _check_rate_limit(self, user_id: str) -> int:
        rate_limit = self.config.get("rate_limit_seconds", 60)
        current_time = time.time()
        if user_id in self.user_query_times:
            last_query_time = self.user_query_times[user_id]
            elapsed = current_time - last_query_time
            if elapsed < rate_limit:
                remaining = int(rate_limit - elapsed)
                return remaining
        self.user_query_times[user_id] = current_time
        return 0
    
    def _get_group_id(self, event: AstrMessageEvent) -> str:
        return event.message_obj.group_id if hasattr(event.message_obj, 'group_id') else ""
    
    async def _handle_name_verification(self, event: AstrMessageEvent, user_input: str):
        user_id = event.get_sender_id()
        
        if user_id not in self.pending_verification:
            return
        
        input_name = user_input.strip()
        if not input_name:
            return
        
        verify_info = self.pending_verification[user_id]
        expected_name = verify_info["user_name"]
        retry_count = verify_info.get("retry_count", 3)
        query_type = verify_info.get("query_type", "device")
        
        if input_name == expected_name:
            if query_type in ["login_log", "fail_log"]:
                result = verify_info.get("result", "")
                yield event.plain_result(result)
            else:
                result = self.pending_reply.get(user_id, "")
                await event.bot.send_private_msg(user_id=int(user_id), message=result)
            
            del self.pending_verification[user_id]
            if user_id in self.pending_reply:
                del self.pending_reply[user_id]
            if user_id in self.pending_login_log:
                del self.pending_login_log[user_id]
            if user_id in self.pending_fail_log:
                del self.pending_fail_log[user_id]
            
            logger.info(f"用户 [{user_id}] 姓名验证成功")
        else:
            retry_count -= 1
            
            if retry_count <= 0:
                del self.pending_verification[user_id]
                if user_id in self.pending_reply:
                    del self.pending_reply[user_id]
                if user_id in self.pending_login_log:
                    del self.pending_login_log[user_id]
                if user_id in self.pending_fail_log:
                    del self.pending_fail_log[user_id]
                
                account_type = verify_info.get("account_type", "账号")
                yield event.plain_result(f"{account_type}姓名验证失败次数过多。请稍后再试。")
                logger.info(f"用户 [{user_id}] 姓名验证失败，已达最大重试次数")
            else:
                self.pending_verification[user_id]["retry_count"] = retry_count
                account_type = verify_info.get("account_type", "账号")
                yield event.plain_result(f"姓名不匹配，请重新输入（剩余 {retry_count} 次尝试机会）:\n请输入该{account_type}登记的姓名:")
    
    @filter.event_message_type(EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        user_id = event.get_sender_id()
        group_id = self._get_group_id(event)
        is_group = bool(group_id)
        
        if user_id in self.pending_verification:
            event.stop_event()
            async for ret in self._handle_name_verification(event, message_str):
                yield ret
            return
        
        if user_id in self.pending_user_type_selection:
            event.stop_event()
            await self._handle_user_type_selection(event, message_str)
            return
        
        if user_id in self.pending_login_log:
            event.stop_event()
            async for ret in self._handle_login_log_input(event, message_str):
                yield ret
            return
        
        if user_id in self.pending_fail_log:
            event.stop_event()
            async for ret in self._handle_fail_log_input(event, message_str):
                yield ret
            return
        
        if user_id in self.pending_users:
            event.stop_event()
            selected_type = self.user_selected_type.get(user_id, "")
            
            if selected_type == "教职工":
                await self._process_teacher_query(event, message_str)
            else:
                extracted_id = self.extract_student_id(message_str)
                if extracted_id:
                    await self._process_query(event, extracted_id)
                else:
                    yield event.plain_result(self.get_error_message_for_invalid_format(selected_type))
            return
        
        account_id = self.extract_student_id(message_str)
        query_account_id = self._extract_id_from_query(message_str)
        
        if is_group:
            if query_account_id:
                event.stop_event()
                try:
                    await event.bot.send_private_msg(
                        user_id=int(user_id),
                        message="已收到查询请求，正在处理..."
                    )
                    await self._process_query(event, query_account_id)
                    try:
                        nickname = event.get_sender_nickname() if hasattr(event, 'get_sender_nickname') else str(user_id)
                    except:
                        nickname = str(user_id)
                    yield event.plain_result(f"@ {nickname} 已通过私聊为您处理查询请求")
                except Exception as e:
                    logger.warning(f"发送私聊失败: {e}")
                    yield event.plain_result("请先添加机器人为好友后再使用此功能")
                return
            
            if self._is_trigger(message_str):
                try:
                    await event.bot.send_private_msg(
                        user_id=int(user_id),
                        message=self.get_user_type_selection_prompt()
                    )
                except Exception as e:
                    logger.warning(f"发送私聊失败: {e}")
                event.stop_event()
                return
        
        if account_id or query_account_id:
            event.stop_event()
            target_id = account_id if account_id else query_account_id
            await self._process_query(event, target_id)
            return
        
        if self._is_login_log_trigger(message_str):
            event.stop_event()
            self.pending_login_log[user_id] = {"retry_count": 3}
            yield event.plain_result("请输入您要查询的账号")
            return
        
        if self._is_fail_log_trigger(message_str):
            event.stop_event()
            self.pending_fail_log[user_id] = {"retry_count": 3}
            yield event.plain_result("请输入您要查询的账号")
            return
        
        if self._is_trigger(message_str):
            event.stop_event()
            self.pending_user_type_selection.add(user_id)
            yield event.plain_result(self.get_user_type_selection_prompt())
            return
    
    def extract_student_id(self, message: str) -> str:
        patterns = [
            r'1043\d{7}',
            r'5\d{5}',
            r'H[A-Za-z0-9]{6}',
            r'202[4-9]\d{8}'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, message)
            if match:
                return match.group(0)
        return ""
    
    def validate_account_format(self, account: str) -> tuple[bool, str]:
        # 验证账号格式并返回账号类型
        if re.match(r'^1043\d{7}$', account):
            return (True, "研究生账号")
        elif re.match(r'^5\d{5}$', account):
            return (True, "正式编制教师账号")
        elif re.match(r'^H[A-Za-z0-9]{6}$', account):
            return (True, "合同工教师账号")
        elif re.match(r'^202[4-9]\d{8}$', account):
            return (True, "学生账号")
        else:
            return (False, "未知账号类型")
    
    def get_account_type_description(self) -> str:
        return ("请发送账号进行查询")
    
    def get_user_type_selection_prompt(self) -> str:
        return ("请选择您的身份类型：\n1. 本科生\n2. 研究生\n3. 教职工\n请回复数字（1/2/3）或直接回复类型名称")
    
    async def _handle_user_type_selection(self, event: AstrMessageEvent, user_input: str):
        user_id = event.get_sender_id()
        user_input = user_input.strip()
        
        valid_types = {
            "1": "本科生",
            "2": "研究生", 
            "3": "教职工",
            "本科生": "本科生",
            "研究生": "研究生",
            "教职工": "教职工"
        }
        
        selected_type = valid_types.get(user_input)
        
        if not selected_type:
            self.pending_user_type_selection.add(user_id)
            await event.bot.send_private_msg(
                user_id=int(user_id),
                message="无效的选择，请重新选择：\n1. 本科生\n2. 研究生\n3. 教职工\n请回复数字（1/2/3）或直接回复类型名称"
            )
            return
        
        self.pending_user_type_selection.discard(user_id)
        self.pending_users.add(user_id)
        self.user_selected_type[user_id] = selected_type
        
        if selected_type == "教职工":
            await event.bot.send_private_msg(
                user_id=int(user_id),
                message=f"您选择了【教职工】，请输入工号进行查询"
            )
        else:
            await event.bot.send_private_msg(
                user_id=int(user_id),
                message=f"您选择了【{selected_type}】，请输入学号进行查询"
            )
    
    async def _process_teacher_query(self, event: AstrMessageEvent, work_id: str):
        user_id = event.get_sender_id()
        work_id = work_id.strip()
        
        self.pending_users.discard(user_id)
        if user_id in self.user_selected_type:
            del self.user_selected_type[user_id]
        
        remaining = self._check_rate_limit(user_id)
        if remaining > 0:
            await event.bot.send_private_msg(
                user_id=int(user_id),
                message=f"查询过于频繁，请 {remaining} 秒后再试"
            )
            return
        
        logger.info(f"查询教职工 [{work_id}] 的在线设备")
        status, user_name, result = await self.query_devices(work_id)
        
        if status == "error":
            await event.bot.send_private_msg(user_id=int(user_id), message=result)
            return
        
        if status == "offline":
            await event.bot.send_private_msg(
                user_id=int(user_id),
                message=f"工号 {work_id} 无设备在线"
            )
            return
        
        if status == "online":
            await event.bot.send_private_msg(user_id=int(user_id), message=result)
            return
    
    async def _handle_login_log_input(self, event: AstrMessageEvent, user_input: str):
        user_id = event.get_sender_id()
        user_input = user_input.strip()
        
        if not user_input:
            self.pending_login_log[user_id] = {"retry_count": 3}
            yield event.plain_result("请输入您要查询的账号")
            return
        
        is_valid, account_type = self.validate_account_format(user_input)
        if not is_valid:
            retry_count = self.pending_login_log[user_id].get("retry_count", 3)
            retry_count -= 1
            
            if retry_count <= 0:
                del self.pending_login_log[user_id]
                yield event.plain_result("输入错误次数过多，请重新发送\"上线日志\"触发查询")
                return
            else:
                self.pending_login_log[user_id]["retry_count"] = retry_count
                yield event.plain_result(f"账号格式错误，请重新输入（剩余 {retry_count} 次尝试机会）")
                return
        
        remaining = self._check_rate_limit(user_id)
        if remaining > 0:
            yield event.plain_result(f"查询过于频繁，请 {remaining} 秒后再试")
            del self.pending_login_log[user_id]
            return
        
        logger.info(f"查询账号 [{user_input}] 的登录日志")
        user_name = await self._query_account_name(user_input)
        
        status, _, result = await self._query_login_log_for_verification(user_input)
        
        if status == "error":
            yield event.plain_result(result)
            del self.pending_login_log[user_id]
            return
        
        if status == "offline":
            yield event.plain_result(result)
            del self.pending_login_log[user_id]
            return
        
        if status == "online":
            self.pending_verification[user_id] = {
                "account_id": user_input,
                "account_type": account_type,
                "user_name": user_name,
                "retry_count": 3,
                "query_type": "login_log",
                "result": result
            }
            self.pending_login_log[user_id] = {"awaiting_name": True}
            yield event.plain_result("请输入该账号登记的姓名进行验证:")
            return
    
    async def _query_login_log_for_verification(self, username: str):
        sam_url = self.config.get("sam_url", "https://172.17.21.115:8443/sam/services/samapi")
        admin_user = self.config.get("admin_user", "zzpt")
        admin_pass = self.config.get("admin_pass", "Zzpt@0923")
        
        auth_str = f"{admin_user}:{admin_pass}"
        base64_auth = base64.b64encode(auth_str.encode()).decode('utf-8')
        
        from_login_time, to_login_time = self._get_query_days_range()
        from_logout_time, to_logout_time = self._get_query_days_range()
        
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": f"Basic {base64_auth}",
            "SOAPAction": "http://api.spl.ruijie.com/queryOnlineDetailV2"
        }
        
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
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    sam_url,
                    data=soap_body,
                    headers=headers,
                    verify_ssl=False,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        return ("error", None, f"请求失败！HTTP 状态码: {response.status}")
                    
                    xml_text = await response.text()
                    return self._parse_login_log_for_verification(xml_text, username)
                    
        except aiohttp.ClientError:
            return ("error", None, "连接错误：无法连接到 SAM 服务器")
        except Exception as e:
            return ("error", None, f"发生未知错误: {e}")
    
    def _parse_login_log_for_verification(self, xml_text: str, target_username: str):
        try:
            root = ET.fromstring(xml_text)
            
            error_code_elems = root.findall(".//errorCode")
            
            if not error_code_elems:
                return ("error", None, "解析失败：无法找到错误码节点")
            
            error_code = error_code_elems[0].text
            
            if error_code == "0":
                online_details = root.findall(".//onlindetailInfo")
                
                if online_details:
                    total_elems = root.findall(".//total")
                    total = total_elems[0].text if total_elems else "0"
                    user_name = None
                    for detail in online_details:
                        account_id = detail.find('accountId')
                        if account_id is not None and account_id.text:
                            user_name = account_id.text.split('@')[0] if '@' in account_id.text else account_id.text
                            break
                    
                    result = self._format_login_log_result(online_details, total)
                    return ("online", user_name, result)
                else:
                    return ("offline", None, "近三天内无登录日志记录")
            else:
                error_msg_elems = root.findall(".//errorMessage")
                error_msg_text = error_msg_elems[0].text if error_msg_elems else "无详细信息"
                return ("error", None, f"接口返回错误 [代码: {error_code}]: {error_msg_text}")
                
        except ET.ParseError as e:
            return ("error", None, f"XML 解析错误: {e}")
    
    def _format_login_log_result(self, online_details, total):
        result = f"📋 登录日志查询结果（共 {total} 条，近三天）\n" + "="*60 + "\n"
        
        for i, detail in enumerate(online_details):
            login_time = detail.find('loginTime').text if detail.find('loginTime') is not None else 'N/A'
            logout_time = detail.find('logoutTime').text if detail.find('logoutTime') is not None else 'N/A'
            
            if login_time != 'N/A' and login_time:
                login_time = login_time.replace('T', ' ').replace('+08:00', '').replace('.000+08:00', '')
            if logout_time != 'N/A' and logout_time:
                logout_time = logout_time.replace('T', ' ').replace('+08:00', '').replace('.000+08:00', '')
            
            online_sec = int(detail.find('onlineSec').text if detail.find('onlineSec') is not None else '0')
            hours = online_sec // 3600
            minutes = (online_sec % 3600) // 60
            seconds = online_sec % 60
            duration_str = f"{hours}小时{minutes}分{seconds}秒" if hours > 0 else f"{minutes}分{seconds}秒"
            
            user_ipv4 = detail.find('userIpv4').text if detail.find('userIpv4') is not None else 'N/A'
            user_mac = detail.find('userMac').text if detail.find('userMac') is not None else 'N/A'
            terminal_type = detail.find('terminalTypeDes').text if detail.find('terminalTypeDes') is not None else 'N/A'
            area_name = detail.find('areaName').text if detail.find('areaName') is not None else 'N/A'
            service_id = detail.find('serviceId').text if detail.find('serviceId') is not None else 'N/A'
            terminate_cause = detail.find('terminateCause').text if detail.find('terminateCause') is not None else 'N/A'
            
            result += f"🔹 日志 {i+1}\n"
            result += f"   账号:      {detail.find('userId').text if detail.find('userId') is not None else 'N/A'}\n"
            result += f"   登录IP:    {user_ipv4}\n"
            result += f"   MAC地址:   {user_mac}\n"
            result += f"   设备类型:  {terminal_type}\n"
            result += f"   区域:      {area_name}\n"
            result += f"   套餐:      {service_id}\n"
            result += f"   登录时间:  {login_time}\n"
            result += f"   下线时间:  {logout_time}\n"
            result += f"   在线时长:  {duration_str}\n"
            if terminate_cause and terminate_cause != 'N/A':
                result += f"   下线原因:  {terminate_cause}\n"
            result += "   " + "-"*55 + "\n"
        
        return result
    
    async def _query_account_name(self, username: str) -> str:
        logger.info(f"查询账号 [{username}] 的用户信息")
        
        sam_url = self.config.get("sam_url", "https://172.17.21.115:8443/sam/services/samapi")
        admin_user = self.config.get("admin_user", "zzpt")
        admin_pass = self.config.get("admin_pass", "Zzpt@0923")
        
        auth_str = f"{admin_user}:{admin_pass}"
        base64_auth = base64.b64encode(auth_str.encode()).decode('utf-8')
        
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": f"Basic {base64_auth}",
            "SOAPAction": "http://api.spl.ruijie.com/queryAccountProfiles"
        }
        
        soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <queryAccountProfiles>
      <accountId>{username}</accountId>
    </queryAccountProfiles>
  </soap:Body>
</soap:Envelope>
"""
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    sam_url,
                    data=soap_body,
                    headers=headers,
                    verify_ssl=False,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        return None
                    
                    xml_text = await response.text()
                    return self._parse_account_name(xml_text)
                    
        except Exception:
            return None
    
    def _parse_account_name(self, xml_text: str) -> str:
        try:
            root = ET.fromstring(xml_text)
            
            error_code_elems = root.findall(".//errorCode")
            if not error_code_elems:
                return None
            
            error_code = error_code_elems[0].text
            if error_code != "0":
                return None
            
            user_name_elems = root.findall(".//userName")
            if user_name_elems and user_name_elems[0].text:
                return user_name_elems[0].text.strip()
            
            return None
                
        except ET.ParseError:
            return None
    
    def _get_query_days_range(self):
        query_days = self.config.get("query_days", 3)
        tz = pytz.utc
        now = datetime.now(tz)
        to_time = now
        from_time = now - timedelta(days=query_days)
        return from_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", to_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    
    def _is_feature_enabled(self, feature: str) -> bool:
        feature_map = {
            "device": "enable_device_query",
            "login_log": "enable_login_log_query",
            "fail_log": "enable_fail_log_query"
        }
        config_key = feature_map.get(feature, "")
        if config_key:
            return self.config.get(config_key, True)
        return True
    
    async def _handle_fail_log_input(self, event: AstrMessageEvent, user_input: str):
        user_id = event.get_sender_id()
        user_input = user_input.strip()
        
        if not user_input:
            self.pending_fail_log[user_id] = {"retry_count": 3}
            yield event.plain_result("请输入您要查询的账号")
            return
        
        is_valid, account_type = self.validate_account_format(user_input)
        if not is_valid:
            retry_count = self.pending_fail_log[user_id].get("retry_count", 3)
            retry_count -= 1
            
            if retry_count <= 0:
                del self.pending_fail_log[user_id]
                yield event.plain_result("输入错误次数过多，请重新发送\"失败日志\"触发查询")
                return
            else:
                self.pending_fail_log[user_id]["retry_count"] = retry_count
                yield event.plain_result(f"账号格式错误，请重新输入（剩余 {retry_count} 次尝试机会）")
                return
        
        remaining = self._check_rate_limit(user_id)
        if remaining > 0:
            yield event.plain_result(f"查询过于频繁，请 {remaining} 秒后再试")
            del self.pending_fail_log[user_id]
            return
        
        logger.info(f"查询账号 [{user_input}] 的失败日志")
        user_name = await self._query_account_name(user_input)
        
        status, _, result = await self._query_fail_log_for_verification(user_input)
        
        if status == "error":
            yield event.plain_result(result)
            del self.pending_fail_log[user_id]
            return
        
        if status == "offline":
            yield event.plain_result(result)
            del self.pending_fail_log[user_id]
            return
        
        if status == "online":
            self.pending_verification[user_id] = {
                "account_id": user_input,
                "account_type": account_type,
                "user_name": user_name,
                "retry_count": 3,
                "query_type": "fail_log",
                "result": result
            }
            self.pending_fail_log[user_id] = {"awaiting_name": True}
            yield event.plain_result("请输入该账号登记的姓名进行验证:")
            return
    
    async def _query_fail_log_for_verification(self, username: str):
        sam_url = self.config.get("sam_url", "https://172.17.21.115:8443/sam/services/samapi")
        admin_user = self.config.get("admin_user", "zzpt")
        admin_pass = self.config.get("admin_pass", "Zzpt@0923")
        
        auth_str = f"{admin_user}:{admin_pass}"
        base64_auth = base64.b64encode(auth_str.encode()).decode('utf-8')
        
        from_date, to_date = self._get_query_days_range()
        
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": f"Basic {base64_auth}",
            "SOAPAction": "http://api.spl.ruijie.com/queryLoginFailLog"
        }
        
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
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    sam_url,
                    data=soap_body,
                    headers=headers,
                    verify_ssl=False,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        return ("error", None, f"请求失败！HTTP 状态码: {response.status}")
                    
                    xml_text = await response.text()
                    return self._parse_fail_log_for_verification(xml_text, username)
                    
        except aiohttp.ClientError:
            return ("error", None, "连接错误：无法连接到 SAM 服务器")
        except Exception as e:
            return ("error", None, f"发生未知错误: {e}")
    
    def _parse_fail_log_for_verification(self, xml_text: str, target_username: str):
        try:
            root = ET.fromstring(xml_text)
            
            error_code_elems = root.findall(".//errorCode")
            
            if not error_code_elems:
                return ("error", None, "解析失败：无法找到错误码节点")
            
            error_code = error_code_elems[0].text
            
            if error_code == "0":
                fail_logs = root.findall(".//loginFailLog")
                total_elems = root.findall(".//total")
                total = total_elems[0].text if total_elems else "0"
                
                if not fail_logs:
                    return ("offline", None, f"共找到 0 条失败日志（近三天）")
                
                result = self._format_fail_log_result(fail_logs, total)
                user_name = None
                for log in fail_logs:
                    msg = log.find('msg').text if log.find('msg') is not None else 'N/A'
                    if msg and msg != 'N/A':
                        try:
                            parts = msg.split(', ')
                            for part in parts:
                                if ':' in part:
                                    key_value = part.split(':', 1)
                                    if len(key_value) == 2 and key_value[0].strip() == '用户':
                                        user_name = key_value[1].strip().strip('()')
                                        break
                            if user_name:
                                break
                        except Exception:
                            pass
                
                return ("online", user_name, result)
            else:
                error_msg_elems = root.findall(".//errorMessage")
                error_msg_text = error_msg_elems[0].text if error_msg_elems else "无详细信息"
                return ("error", None, f"接口返回错误 [代码: {error_code}]: {error_msg_text}")
                
        except ET.ParseError as e:
            return ("error", None, f"XML 解析错误: {e}")
    
    def _format_fail_log_result(self, fail_logs, total):
        result = f"❌ 失败日志查询结果（共 {total} 条，近三天）\n" + "="*60 + "\n"
        
        for i, log in enumerate(fail_logs):
            create_time = log.find('createTime').text if log.find('createTime') is not None else 'N/A'
            msg = log.find('msg').text if log.find('msg') is not None else 'N/A'
            
            if create_time != 'N/A' and create_time:
                create_time = create_time.replace('T', ' ').replace('+08:00', '')
            
            user_id = 'N/A'
            area = 'N/A'
            service = 'N/A'
            access_type = 'N/A'
            user_ipv4 = 'N/A'
            user_mac = 'N/A'
            location = 'N/A'
            reason = 'N/A'
            
            if msg and msg != 'N/A':
                try:
                    parts = msg.split(', ')
                    for part in parts:
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
                                elif key == 'NAS IPv4':
                                    pass
                                elif key == '用户IPv4':
                                    user_ipv4 = value
                                elif key == 'MAC':
                                    user_mac = value
                                elif key == '接入位置描述':
                                    location = value
                                elif key == '原因':
                                    reason = value
                except Exception:
                    pass
            
            result += f"🔸 失败 {i+1}\n"
            result += f"   账号:      {user_id}\n"
            result += f"   失败时间:  {create_time}\n"
            result += f"   失败IP:    {user_ipv4}\n"
            result += f"   MAC地址:   {user_mac}\n"
            result += f"   接入方式:  {access_type}\n"
            result += f"   区域:      {area}\n"
            result += f"   服务:      {service}\n"
            result += f"   位置:      {location}\n"
            result += f"   原因:      {reason}\n"
            result += "   " + "-"*55 + "\n"
        
        return result
    
    def get_error_message_for_invalid_format(self, selected_type: str = "") -> str:
        if selected_type == "教职工":
            return (f"工号格式错误！")
        elif selected_type in ["本科生", "研究生"]:
            return (f"学号格式错误！")
        return (f"账号格式错误！")
    
    async def query_devices(self, username: str) -> str:
        logger.info(f"查询用户 [{username}] 的在线设备")
        
        sam_url = self.config.get("sam_url", "https://172.17.21.115:8443/sam/services/samapi")
        admin_user = self.config.get("admin_user", "zzpt")
        admin_pass = self.config.get("admin_pass", "Zzpt@0923")
        
        auth_str = f"{admin_user}:{admin_pass}"
        base64_auth = base64.b64encode(auth_str.encode()).decode('utf-8')
        
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": f"Basic {base64_auth}",
            "SOAPAction": "http://api.spl.ruijie.com/queryOnlineUserV2"
        }
        
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
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    sam_url,
                    data=soap_body,
                    headers=headers,
                    verify_ssl=False,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        return f"请求失败！HTTP 状态码: {response.status}"
                    
                    xml_text = await response.text()
                    return self._parse_for_verification(xml_text, username)
                    
        except aiohttp.ClientError:
            return ("error", "连接错误：无法连接到 SAM 服务器", None)
        except Exception as e:
            return ("error", f"发生未知错误: {e}", None)
    
    def _parse_for_verification(self, xml_text, target_username):
        try:
            root = ET.fromstring(xml_text)
            
            error_code_elems = root.findall(".//errorCode")
            
            if not error_code_elems:
                return ("error", "解析失败：无法找到错误码节点。", None)
            
            error_code = error_code_elems[0].text
            
            if error_code == "0":
                online_user_infos = root.findall(".//onlineUserInfosV2")
                
                if online_user_infos:
                    user_name = None
                    for online_user_info in online_user_infos:
                        name_elem = online_user_info.find('userName')
                        if name_elem is not None and name_elem.text:
                            user_name = name_elem.text.strip()
                            break
                    
                    result = self._format_result(online_user_infos, target_username)
                    return ("online", user_name, result)
                else:
                    return ("offline", None, None)
            else:
                error_msg_elems = root.findall(".//errorMessage")
                error_msg_text = error_msg_elems[0].text if error_msg_elems else "无详细信息"
                return ("error", None, f"接口返回错误 [代码: {error_code}]: {error_msg_text}")
                
        except ET.ParseError as e:
            return ("error", None, f"XML 解析错误: {e}")
    
    def _format_result(self, online_user_infos, target_username):
        result = f"共找到 {len(online_user_infos)} 个在线设备：\n" + "-"*60 + "\n"
        
        for i, online_user_info in enumerate(online_user_infos):
            result += f"设备 {i+1}:\n"
            result += f"  用户名:      {online_user_info.find('userId').text if online_user_info.find('userId') is not None else 'N/A'}\n"
            result += f"  MAC 地址:    {online_user_info.find('userMac').text if online_user_info.find('userMac') is not None else 'N/A'}\n"
            result += f"  IP 地址:     {online_user_info.find('userIpv4').text if online_user_info.find('userIpv4') is not None else 'N/A'}\n"
            result += f"  设备类型:    {online_user_info.find('terminalTypeDes').text if online_user_info.find('terminalTypeDes') is not None else 'N/A'}\n"
            result += f"  上线时间:    {online_user_info.find('onlineTime').text if online_user_info.find('onlineTime') is not None else 'N/A'}\n"
            result += f"  区域:        {online_user_info.find('areaName').text if online_user_info.find('areaName') is not None else 'N/A'}\n"
            result += f"  套餐:        {online_user_info.find('serviceId').text if online_user_info.find('serviceId') is not None else 'N/A'}\n"
            result += "-" * 60 + "\n"
        
        return result
    
    async def terminate(self):
        self.pending_users.clear()
        self.user_query_times.clear()
        self.pending_verification.clear()
        self.pending_reply.clear()
        self.pending_user_type_selection.clear()
        self.user_selected_type.clear()
        self.pending_login_log.clear()
        self.pending_fail_log.clear()
    
    def _get_query_days_range(self):
        query_days = self.config.get("query_days", 3)
        tz = pytz.utc
        now = datetime.now(tz)
        to_time = now
        from_time = now - timedelta(days=query_days)
        return from_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", to_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    
    async def query_online_detail(self, username: str) -> str:
        logger.info(f"查询用户 [{username}] 的登录日志")
        
        sam_url = self.config.get("sam_url", "https://172.17.21.115:8443/sam/services/samapi")
        admin_user = self.config.get("admin_user", "zzpt")
        admin_pass = self.config.get("admin_pass", "Zzpt@0923")
        
        auth_str = f"{admin_user}:{admin_pass}"
        base64_auth = base64.b64encode(auth_str.encode()).decode('utf-8')
        
        from_login_time, to_login_time = self._get_query_days_range()
        from_logout_time, to_logout_time = self._get_query_days_range()
        
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": f"Basic {base64_auth}",
            "SOAPAction": "http://api.spl.ruijie.com/queryOnlineDetailV2"
        }
        
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
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    sam_url,
                    data=soap_body,
                    headers=headers,
                    verify_ssl=False,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        return f"请求失败！HTTP 状态码: {response.status}"
                    
                    xml_text = await response.text()
                    return self._parse_online_detail(xml_text)
                    
        except aiohttp.ClientError:
            return "连接错误：无法连接到 SAM 服务器"
        except Exception as e:
            return f"发生未知错误: {e}"
    
    def _parse_online_detail(self, xml_text: str) -> str:
        try:
            root = ET.fromstring(xml_text)
            
            error_code_elems = root.findall(".//errorCode")
            
            if not error_code_elems:
                return "解析失败：无法找到错误码节点"
            
            error_code = error_code_elems[0].text
            
            if error_code == "0":
                online_details = root.findall(".//onlindetailInfo")
                total_elems = root.findall(".//total")
                total = total_elems[0].text if total_elems else "0"
                
                if not online_details:
                    return f"共找到 0 条登录日志（近三天）"
                
                result = f"📋 登录日志查询结果（共 {total} 条，近三天）\n" + "="*60 + "\n"
                
                for i, detail in enumerate(online_details):
                    login_time = detail.find('loginTime').text if detail.find('loginTime') is not None else 'N/A'
                    logout_time = detail.find('logoutTime').text if detail.find('logoutTime') is not None else 'N/A'
                    
                    if login_time != 'N/A' and login_time:
                        login_time = login_time.replace('T', ' ').replace('+08:00', '').replace('.000+08:00', '')
                    if logout_time != 'N/A' and logout_time:
                        logout_time = logout_time.replace('T', ' ').replace('+08:00', '').replace('.000+08:00', '')
                    
                    online_sec = int(detail.find('onlineSec').text if detail.find('onlineSec') is not None else '0')
                    hours = online_sec // 3600
                    minutes = (online_sec % 3600) // 60
                    seconds = online_sec % 60
                    duration_str = f"{hours}小时{minutes}分{seconds}秒" if hours > 0 else f"{minutes}分{seconds}秒"
                    
                    user_ipv4 = detail.find('userIpv4').text if detail.find('userIpv4') is not None else 'N/A'
                    user_mac = detail.find('userMac').text if detail.find('userMac') is not None else 'N/A'
                    terminal_type = detail.find('terminalTypeDes').text if detail.find('terminalTypeDes') is not None else 'N/A'
                    area_name = detail.find('areaName').text if detail.find('areaName') is not None else 'N/A'
                    service_id = detail.find('serviceId').text if detail.find('serviceId') is not None else 'N/A'
                    terminate_cause = detail.find('terminateCause').text if detail.find('terminateCause') is not None else 'N/A'
                    
                    result += f"🔹 日志 {i+1}\n"
                    result += f"   账号:      {detail.find('userId').text if detail.find('userId') is not None else 'N/A'}\n"
                    result += f"   登录IP:    {user_ipv4}\n"
                    result += f"   MAC地址:   {user_mac}\n"
                    result += f"   设备类型:  {terminal_type}\n"
                    result += f"   区域:      {area_name}\n"
                    result += f"   套餐:      {service_id}\n"
                    result += f"   登录时间:  {login_time}\n"
                    result += f"   下线时间:  {logout_time}\n"
                    result += f"   在线时长:  {duration_str}\n"
                    if terminate_cause and terminate_cause != 'N/A':
                        result += f"   下线原因:  {terminate_cause}\n"
                    result += "   " + "-"*55 + "\n"
                
                return result
            else:
                error_msg_elems = root.findall(".//errorMessage")
                error_msg_text = error_msg_elems[0].text if error_msg_elems else "无详细信息"
                return f"接口返回错误 [代码: {error_code}]: {error_msg_text}"
                
        except ET.ParseError as e:
            return f"XML 解析错误: {e}"
    
    async def query_login_fail_log(self, username: str) -> str:
        logger.info(f"查询用户 [{username}] 的失败日志")
        
        sam_url = self.config.get("sam_url", "https://172.17.21.115:8443/sam/services/samapi")
        admin_user = self.config.get("admin_user", "zzpt")
        admin_pass = self.config.get("admin_pass", "Zzpt@0923")
        
        auth_str = f"{admin_user}:{admin_pass}"
        base64_auth = base64.b64encode(auth_str.encode()).decode('utf-8')
        
        from_date, to_date = self._get_query_days_range()
        
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": f"Basic {base64_auth}",
            "SOAPAction": "http://api.spl.ruijie.com/queryLoginFailLog"
        }
        
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
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    sam_url,
                    data=soap_body,
                    headers=headers,
                    verify_ssl=False,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        return f"请求失败！HTTP 状态码: {response.status}"
                    
                    xml_text = await response.text()
                    return self._parse_login_fail_log(xml_text)
                    
        except aiohttp.ClientError:
            return "连接错误：无法连接到 SAM 服务器"
        except Exception as e:
            return f"发生未知错误: {e}"
    
    def _parse_login_fail_log(self, xml_text: str) -> str:
        try:
            root = ET.fromstring(xml_text)
            
            error_code_elems = root.findall(".//errorCode")
            
            if not error_code_elems:
                return "解析失败：无法找到错误码节点"
            
            error_code = error_code_elems[0].text
            
            if error_code == "0":
                fail_logs = root.findall(".//loginFailLog")
                total_elems = root.findall(".//total")
                total = total_elems[0].text if total_elems else "0"
                
                if not fail_logs:
                    return f"共找到 0 条失败日志（近三天）"
                
                result = f"失败日志查询结果（共 {total} 条，近三天）\n" + "="*60 + "\n"
                
                for i, log in enumerate(fail_logs):
                    create_time = log.find('createTime').text if log.find('createTime') is not None else 'N/A'
                    msg = log.find('msg').text if log.find('msg') is not None else 'N/A'
                    
                    if create_time != 'N/A' and create_time:
                        create_time = create_time.replace('T', ' ').replace('+08:00', '')
                    
                    user_id = 'N/A'
                    area = 'N/A'
                    service = 'N/A'
                    access_type = 'N/A'
                    nas_ip = 'N/A'
                    user_ipv4 = 'N/A'
                    user_mac = 'N/A'
                    location = 'N/A'
                    reason = 'N/A'
                    
                    if msg and msg != 'N/A':
                        try:
                            parts = msg.split(', ')
                            for part in parts:
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
                                        elif key == 'NAS IPv4':
                                            nas_ip = value
                                        elif key == '用户IPv4':
                                            user_ipv4 = value
                                        elif key == 'MAC':
                                            user_mac = value
                                        elif key == '接入位置描述':
                                            location = value
                                        elif key == '原因':
                                            reason = value
                        except Exception:
                            pass
                    
                    result += f"🔸 失败 {i+1}\n"
                    result += f"   账号:      {user_id}\n"
                    result += f"   失败时间:  {create_time}\n"
                    result += f"   失败IP:    {user_ipv4}\n"
                    result += f"   MAC地址:   {user_mac}\n"
                    result += f"   接入方式:  {access_type}\n"
                    result += f"   区域:      {area}\n"
                    result += f"   服务:      {service}\n"
                    result += f"   位置:      {location}\n"
                    result += f"   原因:      {reason}\n"
                    result += "   " + "-"*55 + "\n"
                
                return result
            else:
                error_msg_elems = root.findall(".//errorMessage")
                error_msg_text = error_msg_elems[0].text if error_msg_elems else "无详细信息"
                return f"接口返回错误 [代码: {error_code}]: {error_msg_text}"
                
        except ET.ParseError as e:
            return f"XML 解析错误: {e}"
