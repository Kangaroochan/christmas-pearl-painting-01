# 圣诞珍珠画01

把一段生图提示词或一组参考图，改写成批量生成时每张都有明显差异、又看得出是同一个系列的提示词。深度拆解固定层和变量层，每张参考图输出一条可以直接丢进生图工具批量跑的提示词，并存成 TXT，放在参考图旁边。

只写提示词，不出图。出图用 [pinshuai-batch-image](https://github.com/Kangaroochan/pinshuai-batch-image)，它会直接读取这里写好的 TXT。

出图后的反馈会记进 `references/lessons.md`，下次写提示词前先读，少犯同样的错。

## 安装

克隆到 Codex 的技能目录，文件夹名保持 `圣诞珍珠画01`：

```powershell
git clone https://github.com/Kangaroochan/christmas-pearl-painting-01.git "$env:USERPROFILE\.codex\skills\圣诞珍珠画01"
```

可选的素材池脚本 `scripts/sample_prompts.py` 需要 Python 3，只用标准库。

## 使用

在 Codex 里调用 `$圣诞珍珠画01`，给出参考图或原提示词。默认只输出提示词；要素材池、Excel 或"严格保证每两张至少 N 处不同"时再说明。

## 文件

- `SKILL.md`：skill 规则
- `references/lessons.md`：出图经验记录，里面是作者自己的出图记录，可以清空成模板再用
- `references/example_christmas.md`、`references/example_christmas_spec.json`：完整示例
- `scripts/sample_prompts.py`：素材池抽取脚本（可选输出）

## 许可

MIT
