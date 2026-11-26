# Looki 日程管理插件

- 本目录是 AstrBot 插件源码，入口为 `main.py`，元信息与版本见 `metadata.yaml`，配置定义见 `_conf_schema.json`。
- 插件安装到 AstrBot 的 `data/plugins/astrbot_plugin_looki_companion/`；运行数据库位于插件源码之外的 `data/plugin_data/astrbot_plugin_looki_companion/`，更新源码时不得删除该数据目录。
- 日历页面为 `pages/schedule/index.html`；媒体与图片逻辑分别位于 `media_service.py`、`image_service.py`，SQLite 存储位于 `schedule_store.py`。
- 测试使用标准库 `unittest`，网络、模型与外部命令使用 Mock。工作区测试命令：
  ```powershell
  $env:PYTHONPATH = "G:\qiongqiong\项目\AstrBot;G:\qiongqiong\插件\AstrBot个人插件"
  & "G:\qiongqiong\项目\AstrBot\.venv\Scripts\python.exe" -m unittest discover -s tests
  ```
- 修改版本时同步核对 `main.py`、`metadata.yaml` 与 `README.md`。发布包统一放入 `G:\qiongqiong\运维\发布包\astrbot_插件发布包`。
- 一次性探针、调试输出和临时脚本放入 `G:\qiongqiong\临时`，不得让插件正式代码依赖该目录。
