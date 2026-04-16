from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star
from astrbot.api import logger
import aiohttp
import base64
import xml.etree.ElementTree as ET
import re
import time

class UserDevicesPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = context.get_config()
        self.pending_users = set()
        self.user_query_times = {}
        self.pending_verification = {}
        self.pending_reply = {}
        
    def _is_trigger(self, message: str) -> bool:
        keywords = ["在线设备", "查询设备", "设备查询", "在线用户", "查询用户", "用户查询", "zscx", "设备", "用户", "在线", "查询", "cx", "sb", "yh"]
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
        current_time = time.time()
        if user_id in self.user_query_times:
            last_query_time = self.user_query_times[user_id]
            elapsed = current_time - last_query_time
            if elapsed < 60:
                remaining = int(60 - elapsed)
                return remaining
        self.user_query_times[user_id] = current_time
        return 0
    
    def _get_group_id(self, event: AstrMessageEvent) -> str:
        return event.message_obj.group_id if hasattr(event.message_obj, 'group_id') else ""
    
    async def _handle_name_verification(self, event: AstrMessageEvent, user_input: str) -> bool:
        user_id = event.get_sender_id()
        
        if user_id not in self.pending_verification:
            return False
        
        input_name = user_input.strip()
        if not input_name:
            return True
        
        verify_info = self.pending_verification[user_id]
        expected_name = verify_info["user_name"]
        retry_count = verify_info.get("retry_count", 3)
        
        if input_name == expected_name:
            result = self.pending_reply.get(user_id, "")
            await event.bot.send_private_msg(user_id=int(user_id), message=result)
            
            del self.pending_verification[user_id]
            if user_id in self.pending_reply:
                del self.pending_reply[user_id]
            
            logger.info(f"用户 [{user_id}] 姓名验证成功")
            return True
        else:
            retry_count -= 1
            
            if retry_count <= 0:
                del self.pending_verification[user_id]
                if user_id in self.pending_reply:
                    del self.pending_reply[user_id]
                
                account_type = verify_info.get("account_type", "账号")
                await event.bot.send_private_msg(
                    user_id=int(user_id),
                    message=f"{account_type}姓名验证失败次数过多。请稍后再试。"
                )
                logger.info(f"用户 [{user_id}] 姓名验证失败，已达最大重试次数")
            else:
                self.pending_verification[user_id]["retry_count"] = retry_count
                account_type = verify_info.get("account_type", "账号")
                await event.bot.send_private_msg(
                    user_id=int(user_id),
                    message=f"姓名不匹配，请重新输入（剩余 {retry_count} 次尝试机会）:\n请输入该{account_type}登记的姓名:"
                )
            
            return True
    
    @filter.event_message_type(EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        user_id = event.get_sender_id()
        group_id = self._get_group_id(event)
        is_group = bool(group_id)
        
        if user_id in self.pending_verification:
            event.stop_event()
            await self._handle_name_verification(event, message_str)
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
                        message=self.get_account_type_description()
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
        
        if self._is_trigger(message_str):
            event.stop_event()
            self.pending_users.add(user_id)
            yield event.plain_result("请发送账号进行查询\n")
            return
        
        if user_id in self.pending_users:
            self.pending_users.discard(user_id)
            
            extracted_id = self.extract_student_id(message_str)
            if extracted_id:
                await self._process_query(event, extracted_id)
            else:
                yield event.plain_result(self.get_error_message_for_invalid_format(message_str))
            event.stop_event()
    
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
    
    def get_error_message_for_invalid_format(self, user_input: str) -> str:
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
