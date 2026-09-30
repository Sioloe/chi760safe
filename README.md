# 为i发电 · CHI 实时备份

**0.1 验证版，Windows 10/11 x64。** 在原 CHI760E 中设置参数、开始实验并查看曲线，本程序只读已采集的数据，按另一频率写入 TXT。

[下载 Windows 便携包](https://github.com/Sioloe/chi760safe/releases/tag/v0.1.0-preview.1) · [完整使用说明](docs/使用说明.txt)

## 使用

1. 完整解压下载包，保留 `_internal` 文件夹，无需另装 Python。
2. 打开原来的 `chi760e.exe`，再运行 `CHIBackup.exe`。
3. 选择 CHI 进程、保存位置和保存间隔，点击“连接并开始备份”。
4. 回到 CHI 设置并开始 CA、Amperometric i–t 或 CP，观察备份点数是否增长。
5. 测试结束后等备份显示已保存，再照常保存 CHI 原始 BIN。

每次测试使用独立文件夹。默认每秒提交一批新数据点，可设为 0.5–60 秒；CHI 的采样频率仍由原软件控制。

| 测试 | data.txt 中文列 | raw_SI.txt 测量单位 |
| --- | --- | --- |
| CA / i–t | 序号、时间(s)、电流(mA) | A |
| CP | 序号、时间(s)、电位(V) | V |

文件使用 UTF-8 BOM 和 Tab 分列，每行一个采样点。程序不补造未测量的电学量或电荷。TXT 为额外备份，保留原始 BIN 以保存完整实验参数。

## 兼容与验证范围

- 仅适配已核对的 **CHI760E 15.3** 原文件，启动时校验 SHA-256：`cccf827f205e7f27b548252defd53b9be6b2cb8c646a9c4742d1472d1633f347`。其他构建会拒绝读取。
- 已通过 16 项离线检查和便携 EXE 的窗口创建检查。
- 原 CHI 软件加载的 CA 示例共 500 个电流值，与原始 BIN 逐点、逐字节一致。
- CP、i–t 已实现读取适配并通过模拟测试，**原生示例逐点核对尚未完成**。
- **真实仪器采集和物理断电场景尚未验证**。首次使用应先做短时比对。
- 目前只支持主通道单曲线；辅助通道、第二电极和其他技术会提示未支持。

程序约每 0.25 秒检查缓冲区，保留一个采样点余量以避开正在写入的点，结束时补齐最后一点。断电损失窗口还包括保存间隔及系统/硬盘缓存，不能承诺零丢失。若 CHI 缩减了缓冲区，缩减前尚未读取的点无法补回。

适配器只申请进程查询和内存读取权限，不注入、不写入、不暂停 CHI 进程，不连接仪器接口、不发送实验指令。建议继续保留 CHI 自带恢复数据；已结束且解锁的匹配 `.tmp` 可额外转换为 TXT。

## 源码与构建

运行需要 Windows x64、Python 3.12（含 Tcl/Tk），应用本身仅使用标准库。在仓库根目录执行：

```powershell
python app/chi_backup.py
python app/self_check.py build/offline-check
python -m pip install -r requirements-build.txt
python scripts/build_windows.py
```

构建脚本生成 `dist/CHIBackup` 和 ZIP，并运行离线及窗口创建自检；不会开始电化学测试。

源码和包内示例仅含人工模拟数据，不包含 CHI 厂商程序、厂商 BIN 样例或用户实测文件。本工具为独立项目，非 CH Instruments 官方产品。项目尚未指定开源许可证；第三方组件遵循各自许可证。
