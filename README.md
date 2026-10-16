# AstrBot SAM 用户设备查询插件

**版本**: `v1.0.0`  
**作者**: `yzdr`

查询用户在线设备信息的 AstrBot 插件。

## 功能

- 支持通过"在线设备查询"指令查询用户的在线设备信息
- 显示用户名、MAC地址、IP地址、接入设备IP、设备类型、上线时间、区域、套餐等信息

## 安装

1. 将插件目录放到 `data/plugins/` 目录
2. 在 AstrBot WebUI 的插件管理页面安装并启用
3. 在插件配置中填写 SAM 服务器地址和管理员账号密码

## 配置

| 配置项 | 说明 |
|--------|------|
| sam_url | SAM 服务器地址 |
| admin_user | 管理员用户名 |
| admin_pass | 管理员密码 |

## 使用

发送"在线设备查询"指令，然后按提示输入完整学号即可查询。

## 项目结构

```
astrbot_plugin_sam/
├── main.py            # 插件主文件
├── metadata.yaml      # 插件元数据
├── requirements.txt   # 依赖列表
├── _conf_schema.json  # 配置 schema
└── README.md          # 说明文档
```
