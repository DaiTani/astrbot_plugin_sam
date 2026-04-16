from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger
import aiohttp
import base64
import xml.etree.ElementTree as ET
import re

class UserDevicesPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = context.get_config()
        self.user_sessions = {}
        
    def _is_trigger(self, message: str) -> bool:
        keywords = ["在线设备", "查询设备", "设备查询", "在线用户", "查询用户", "用户查询"]
        return any(kw in message for kw in keywords)
        
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        user_id = event.get_sender_id()
        session_type = event.message_type
        
        if not self._is_trigger(message_str):
            return
        
        if session_type == "group":
            await self.context.send_message(
                user_id,
                MessageChain().message("请私信我发送学号进行查询\n（例如202592xxxxxx）")
            )
            event.stop_event()
            return
        
        if "学号" in message_str or self.extract_student_id(message_str):
            if self.extract_student_id(message_str):
                student_id = self.extract_student_id(message_str)
                del self.user_sessions[user_id]
                await self.query_devices(event, student_id)
                event.stop_event()
                return
        
        self.user_sessions[user_id] = "waiting_for_student_id"
        await self.context.send_message(
            event.unified_msg_origin,
            MessageChain().message("请发送学号进行查询\n（例如202592xxxxxx）")
        )
        event.stop_event()
    
    def extract_student_id(self, message: str) -> str:
        match = re.search(r'202[4-9]\d{8}', message)
        if match:
            return match.group(0)
        return ""
    
    async def query_devices(self, event: AstrMessageEvent, username: str):
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
                        await self.context.send_message(
                            event.get_sender_id(),
                            MessageChain().message(f"❌ 请求失败！HTTP 状态码: {response.status}")
                        )
                        return
                    
                    xml_text = await response.text()
                    result = self.parse_response(xml_text, username)
                    await self.context.send_message(
                        event.get_sender_id(),
                        MessageChain().message(result)
                    )
                    
        except aiohttp.ClientError as e:
            await self.context.send_message(
                event.get_sender_id(),
                MessageChain().message(f"❌ 连接错误：无法连接到 SAM 服务器")
            )
        except Exception as e:
            await self.context.send_message(
                event.get_sender_id(),
                MessageChain().message(f"❌ 发生未知错误: {e}")
            )
    
    def parse_response(self, xml_text, target_username):
        try:
            root = ET.fromstring(xml_text)
            
            error_code_elems = root.findall(".//errorCode")
            
            if not error_code_elems:
                return "❌ 解析失败：无法找到错误码节点。"
            
            error_code = error_code_elems[0].text
            
            if error_code == "0":
                online_user_infos = root.findall(".//onlineUserInfosV2")
                
                if online_user_infos:
                    result = f"共找到 {len(online_user_infos)} 个在线设备：\n" + "-"*60 + "\n"
                    
                    for i, online_user_info in enumerate(online_user_infos):
                        result += f"设备 {i+1}:\n"
                        result += f"  用户名:      {online_user_info.find('userId').text if online_user_info.find('userId') is not None else 'N/A'}\n"
                        result += f"  MAC 地址:    {online_user_info.find('userMac').text if online_user_info.find('userMac') is not None else 'N/A'}\n"
                        result += f"  IP 地址:     {online_user_info.find('userIpv4').text if online_user_info.find('userIpv4') is not None else 'N/A'}\n"
                        result += f"  接入设备IP:  {online_user_info.find('nasIp').text if online_user_info.find('nasIp') is not None else 'N/A'}\n"
                        result += f"  设备类型:    {online_user_info.find('terminalTypeDes').text if online_user_info.find('terminalTypeDes') is not None else 'N/A'}\n"
                        result += f"  上线时间:    {online_user_info.find('onlineTime').text if online_user_info.find('onlineTime') is not None else 'N/A'}\n"
                        result += f"  区域:        {online_user_info.find('areaName').text if online_user_info.find('areaName') is not None else 'N/A'}\n"
                        result += f"  套餐:        {online_user_info.find('serviceId').text if online_user_info.find('serviceId') is not None else 'N/A'}\n"
                        result += "-" * 60 + "\n"
                    
                    return result
                else:
                    return f"用户 {target_username} 无设备在线。"
            else:
                error_msg_elems = root.findall(".//errorMessage")
                error_msg_text = error_msg_elems[0].text if error_msg_elems else "无详细信息"
                return f"接口返回错误 [代码: {error_code}]: {error_msg_text}"
                
        except ET.ParseError as e:
            return f"XML 解析错误: {e}"
    
    async def terminate(self):
        self.user_sessions.clear()
        pass
