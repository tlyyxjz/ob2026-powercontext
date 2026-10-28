# 第三方代码说明（NOTICE）

本目录 `powercontext/` 是 [PowerContext](https://github.com/oceanbase/powercontext)
的源码副本，随本作品一起交付，目的是让评审不必再从其它分支获取依赖。

| 项 | 值 |
|---|---|
| 上游仓库 | https://github.com/oceanbase/powercontext |
| 分支 | `2026-competition` |
| commit | `d994e64077ddc98fd5c18cd78e87e9b80aa5cc1f` |
| 许可证 | Apache License 2.0（见 `LICENSE-powercontext`） |
| 是否修改 | **未修改**，逐文件原样复制 |
| 复制范围 | 上游 `src/powercontext/` 整个包 |

本作品只调用它的公开接口：`open_builtin_contexts`、`BuiltinConfig`、`SQLiteConfig`、
`MemoryEntryInput`、`ArtifactNotFoundError`。

如果你更希望用 pip 管理这个依赖，也可以跳过本目录，按 README 2.2 的备选方式
从 `2026-competition` 分支 `pip install -e ".[builtin]"`，效果相同。
