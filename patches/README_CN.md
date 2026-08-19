# 透支补丁目录

每个官方版本使用独立补丁文件，文件名格式为：

```text
sub2api-overdraft-v<官方版本>-<基线提交短哈希>.patch
```

管理器在临时 Git 工作树中锁定官方 Release 提交，优先选择不高于目标版本的最新补丁，然后执行：

```bash
git apply --3way --ignore-whitespace <patch>
```

补丁应用、编译、数据库兼容检查或健康检查任一步失败，当前程序保持不变。新增官方版本时应先在本机生成并验证对应补丁，再把补丁文件放入本目录。

当前已验证：

- `sub2api-overdraft-v0.1.177-baeac1f3d.patch`：Fork 原生参考版本的历史补丁。
- `sub2api-overdraft-v0.1.178-e0c48a19e.patch`：官方 `0.1.178` 的透支重放补丁。
