# 圣诞珍珠画01

圣诞老人和小鹿珠钻画挂饰那套三张轮播的批量模板：

1. 开场四宫格："10/10 Christmas activity to / do with your mom/sister 🎄"
2. 和妈妈一起做："Ornament Kit 🥹"
3. 挂在树上："that’s our December evenings sorted...🫶🏼"

文案、emoji、人物选角和负面约束固定，产品可保持原固定款或使用可选款式池；同套第2、3张使用同一组。固定内容写在 `template/carousel.json`。脚本给每套、每张画面写一条提示词，场景、机位、景别等可变点的取值由脚本定好并在各套之间轮换，不靠模型自己挑。同时写出可以直接交给 [pinshuai-batch-image](https://github.com/Kangaroochan/pinshuai-batch-image) 的 `jobs.json`，一次出完全部套数。

只写提示词，不出图。别的参考图要做提示词多样化，用 [pinshuai-prompt-diversify](https://github.com/Kangaroochan/pinshuai-prompt-diversify)（本 skill 就是从它复制出来的）。

## 安装

克隆到 Codex 的技能目录，文件夹名保持 `圣诞珍珠画01`：

```powershell
git clone https://github.com/Kangaroochan/christmas-pearl-painting-01.git "$env:USERPROFILE\.codex\skills\圣诞珍珠画01"
```

脚本需要 Python 3，只用标准库。

## 使用

在 Codex 里调用 `$圣诞珍珠画01`，说要几套、什么深度（1～10，默认 10）。也可以直接跑：

```powershell
python scripts/make_batch.py --sets 10 --depth 10 -o D:/出图任务/圣诞01
python scripts/make_batch.py --check
```

## 产品款式池

用户提供造型一致或相似、图案不同的多款产品时，可在模板配置 `product_pool`（`per_set`、`common_description`、`items`；每款有 `name/ref/description`）。每套从池中不重复抽取、均衡轮换；在用户提供的款式间轮换不算换产品。无款式池时保持原有随机结果。完整字段与占位符见 `SKILL.md` 的“改模板”。

```powershell
python scripts/make_batch.py --template D:/素材/carousel.json --sets 10 --seed 456084 -o D:/出图任务/轮换
python scripts/make_batch.py --template D:/素材/carousel.json --sets 1 --products 雪人 姜饼人 -o D:/出图任务/指定
```

指定款数等于每套款数时固定，多于时只在点名款中轮换；未知、重复或不足会报错。产品随机流独立，不改变场景、机位等抽取。

修复画面问题时用适用于全部选项的通用手部/受力/自然排列规则，不删选项、换安全场景或限制构图；改前改后的差异统计不能降低（2026-11-01 用户要求）。用户明确说不要的画面例外，直接从模板删掉。

## 文件

- `SKILL.md`：skill 规则
- `template/carousel.json`：模板；`template/参考图/`、`template/产品图/`：原始截图和产品裁图
- `scripts/make_batch.py`：按模板生成一批；`scripts/sample_prompts.py`：抽取引擎
- `references/lessons.md`：出图经验记录，里面是作者自己的出图记录

## 许可

MIT
