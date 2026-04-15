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
        # 从配置中获取SAM服务器信息
        self.config = context.get_config()
        # 存储用户会话状态，用于跟踪用户是否正在进行设备查询
        self.user_sessions = {}
        
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        '''处理所有消息，支持"在线设备查询"指令'''        
        message_str = event.message_str.strip()
        user_id = event.get_sender_id()
        
        # 检查是否是"在线设备查询"指令
        if "在线设备查询" in message_str:
            # 获取用户的私聊会话ID
            umo = event.unified_msg_origin
            # 存储用户会话状态
            self.user_sessions[user_id] = "waiting_for_student_id"
            # 主动向用户发送私聊消息
            await self.context.send_message(
                umo,
                MessageChain().message("请告知我完整学号：\n（例如202592xxxxxx)")
            )
            return
        
        # 检查用户是否正在等待输入学号
        if user_id in self.user_sessions and self.user_sessions[user_id] == "waiting_for_student_id":
            # 提取学号
            student_id = self.extract_student_id(message_str)
            if student_id:
                # 清除会话状态
                del self.user_sessions[user_id]
                # 查询设备信息
                await self.query_devices(event, student_id)
            else:
                # 提示用户输入正确的学号
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain().message("请输入正确的学号格式，例如202592xxxxxx")
                )
    
    def extract_student_id(self, message: str) -> str:
        '''提取消息中的学号信息'''        
        # 使用正则表达式匹配学号格式
        # 假设学号格式为：202[4-9]开头，后面跟8位数字
        match = re.search(r'202[4-9]\d{8}', message)
        if match:
            return match.group(0)
        return ""
    
    async def query_devices(self, event: AstrMessageEvent, username: str):
        '''查询用户在线设备信息'''        
        logger.info(f"查询用户 [{username}] 的在线设备")
        
        # 获取配置
        sam_url = self.config.get("sam_url", "https://172.17.21.115:8443/sam/services/samapi")
        admin_user = self.config.get("admin_user", "zzpt")
        admin_pass = self.config.get("admin_pass", "Zzpt@0923")
        
        # 构造Basic Auth头
        auth_str = f"{admin_user}:{admin_pass}"
        base64_auth = base64.b64encode(auth_str.encode()).decode('utf-8')
        
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": f"Basic {base64_auth}",
            "SOAPAction": "http://api.spl.ruijie.com/queryOnlineUserV2"
        }
        
        # 构造SOAP请求体
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
                            event.unified_msg_origin,
                            MessageChain().message(f"❌ 请求失败！HTTP 状态码: {response.status}")
                        )
                        return
                    
                    xml_text = await response.text()
                    result = self.parse_response(xml_text, username)
                    await self.context.send_message(
                        event.unified_msg_origin,
                        MessageChain().message(result)
                    )
                    
        except aiohttp.ClientError as e:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain().message(f"❌ 连接错误：无法连接到 SAM 服务器，请检查 IP 地址、端口或网络连接。")
            )
        except Exception as e:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain().message(f"❌ 发生未知错误: {e}")
            )
    
    def parse_response(self, xml_text, target_username):
        try:
            root = ET.fromstring(xml_text)
            
            # 查找错误码节点
            error_code_elems = root.findall(".//errorCode")
            
            if not error_code_elems:
                return "❌ 解析失败：无法找到错误码节点。"
            
            error_code = error_code_elems[0].text
            
            if error_code == "0":
                # 查找在线用户信息节点
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
                # 查找错误消息节点
                error_msg_elems = root.findall(".//errorMessage")
                error_msg_text = error_msg_elems[0].text if error_msg_elems else "无详细信息"
                return f"接口返回错误 [代码: {error_code}]: {error_msg_text}"
                
        except ET.ParseError as e:
            return f"XML 解析错误: {e}"
    
    async def terminate(self):
        '''插件被卸载/停用时调用'''        
        # 清理会话状态
        self.user_sessions.clear()
        pass
