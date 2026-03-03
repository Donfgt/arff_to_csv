# AAM ARFF to CSV Converter

> 将 AAM 三文件标注（`segments/onsets/beatinfo`）转换为可直接用于和声预测训练的 `dataset.csv`。  
> Convert grouped AAM annotations (`segments/onsets/beatinfo`) into a training-ready `dataset.csv`.

---

## 1. 项目定位 / Project Scope

本仓库是从毕业论文工程中拆分的**数据处理子模块**，专注于：

- 解析 AAM 的分组 ARFF 标注；
- 提取旋律、调性与和弦信息；
- 输出统一字段结构的 CSV；
- 产出可复现的质量统计（`aam_summary.json`）。

This repository is an open-source extraction of the thesis preprocessing module. It focuses on:

- parsing grouped AAM ARFF annotations,
- extracting melody/key/chord information,
- exporting a unified CSV schema,
- generating reproducible processing statistics (`aam_summary.json`).

---

## 2. 目录结构 / Repository Structure

```text
arff_to-csv-open-source/
├─ src/
│  └─ aam_to_csv.py                # 核心转换脚本 / Core converter
├─ data/
│  └─ dataset.csv                  # 转换结果样例（可训练）
├─ results/
│  └─ aam_summary.json             # 转换统计报告
├─ docs/
│  └─ references/
│     ├─ arff方案.md
│     └─ 第3章 数据准备与预处理(AAM).md
├─ requirements.txt
└─ README.md
```

---

## 3. 模块使用说明 / Module Usage

### 3.1 环境依赖 / Requirements

- Python 3.10+
- `music21`库

安装依赖 / Install:

```bash
pip install -r requirements.txt
```

### 3.2 运行方式 / Run

#### 示例1：常用运行（含统计文件） / Example 1: Typical run with summary

```bash
python src/aam_to_csv.py \
  --aam_dir "<AAM标注目录>" \
  --output_csv "data/dataset.csv" \
  --summary_json "results/aam_summary.json" \
  --id_start 0001 \
  --id_end 0600 \
  --log_every 50
```

#### 示例2：全量范围（默认区间） / Example 2: Full range (default range)

```bash
python src/aam_to_csv.py \
  --aam_dir "<AAM标注目录>" \
  --output_csv "data/dataset.csv"
```

> 说明 / Note：`0001~0600` 仅为本文示例运行范围，不是固定限制。

### 3.3 CLI 参数总览 / Full CLI Argument List

| 参数 | 必填 | 默认值 | 说明（中文） | Description (EN) |
|---|---|---|---|---|
| `--aam_dir` | 是 | - | AAM 分组标注目录（含 `xxxx_segments/onsets/beatinfo`） | Input folder containing grouped AAM files |
| `--output_csv` | 是 | - | 输出 CSV 路径 | Output CSV path |
| `--summary_json` | 否 | `""` | 可选统计报告输出路径 | Optional summary JSON output path |
| `--id_start` | 否 | `0001` | 起始组编号（包含） | Start group id (inclusive) |
| `--id_end` | 否 | `9999` | 结束组编号（包含） | End group id (inclusive) |
| `--no_normalize_key` | 否 | 关闭 | 禁用调性归一化（默认会归一到 C/Am） | Disable key normalization (default is enabled) |
| `--allow_txt` | 否 | 关闭 | 允许读取 `.txt`（其内容需为 ARFF 格式） | Allow `.txt` files containing ARFF content |
| `--log_every` | 否 | `50` | 每处理 N 组打印一次进度日志；`<=0` 表示仅结尾输出 | Print progress every N groups; `<=0` means end-only logging |

### 3.4 CLI 快速参考 / Quick CLI Recipes

- 小范围联调 / Small-range debug:
  ```bash
  python src/aam_to_csv.py --aam_dir "<dir>" --output_csv "data/debug.csv" --id_start 0001 --id_end 0010 --log_every 1
  ```
- 保持原调（不归一）/ Keep original key (no normalization):
  ```bash
  python src/aam_to_csv.py --aam_dir "<dir>" --output_csv "data/no_norm.csv" --no_normalize_key
  ```
- 兼容 `.txt` 标注 / Accept `.txt` annotation files:
  ```bash
  python src/aam_to_csv.py --aam_dir "<dir>" --output_csv "data/from_txt.csv" --allow_txt
  ```


---

## 4. 处理流程说明 / Processing Pipeline

1. **分组识别 / Group discovery**  
   按文件名匹配 `xxxx_segments`、`xxxx_onsets`、`xxxx_beatinfo`。

2. **ARFF解析 / ARFF parsing**  
   采用手工解析器读取头与数据区，兼容部分非标准写法。

3. **旋律声部确定 / Melody instrument selection**  
   优先使用 `Generator` 中的 melody 角色；缺失时采用“非打击乐最高平均音高”回退策略。

4. **节拍对齐 / Beat alignment**  
   以 beatinfo 为时间主轴，在 onsets 中查最近时刻，提取旋律音高。

5. **和弦标签转换 / Chord-to-Roman conversion**  
   利用 `music21` 将原始和弦转换为罗马数字标签，异常回退到 `N`。

6. **调性归一 / Key normalization**  
   默认将大调归一到 `C`、小调归一到 `Am`，保留 `transpose_semitones` 便于还原。

7. **写出CSV与统计 / Export CSV & summary**  
   输出训练数据和汇总报告（成功/失败组数、行数、REST占比、N占比等）。

---

## 5. 字段说明 / CSV Field Specification

| 字段名 | 说明（中文） | Description (EN) |
|---|---|---|
| `piece_id` | 乐曲分组ID（如0001） | Group/piece ID |
| `offset` | 事件序号偏移（拍级） | Beat-level event offset |
| `duration` | 当前事件时值（四分音符单位） | Event duration in quarter-note units |
| `measure` | 小节号（按拍推导） | Measure index (derived) |
| `key` | 当前调性（与`key_normalized`一致） | Current key (same as `key_normalized`) |
| `key_original` | 原始调性 | Original key |
| `key_normalized` | 归一化调性（默认C/Am） | Normalized key (C/Am by default) |
| `transpose_semitones` | 归一化移调半音数 | Semitone shift for normalization |
| `melody_pitch` | 归一化后旋律音名 | Normalized melody pitch name |
| `melody_midi` | 归一化后旋律MIDI | Normalized melody MIDI |
| `melody_pitch_original` | 原始旋律音名 | Original melody pitch name |
| `melody_midi_original` | 原始旋律MIDI（无音为-1） | Original melody MIDI (`-1` for rest) |
| `chord_label` | 罗马数字和弦标签 | Roman-numeral chord label |
| `chord_label_raw` | 源数据原始和弦名 | Raw chord label from source |

---

## 6. 效果说明 / Processing Results

当前仓库内示例结果（`data/dataset.csv` + `results/aam_summary.json`）：

- 处理分组范围 / ID range: `0001` ~ `0600`
- 成功组数 / Success groups: `600`
- 失败组数 / Failed groups: `0`
- 总行数 / Total rows: `174,948`
- `N` 标签行数 / `N` rows: `36,622`
- 休止行数 / Rest rows: `55,732`
- 唯一和弦标签数 / Unique chord labels: `13`

说明：`N` 与 `REST` 的存在来自源标注可解析性与旋律时刻无音符情况，属于预期数据特征。  
Note: `N` and `REST` are expected characteristics due to source-label parseability and melody silence at some beat locations.

---

## 7. 版权说明 / Copyright Notice

### 7.1 本仓库代码与派生文档 / Code & derived docs

- `src/aam_to_csv.py` 与本仓库文档由作者整理发布。

### 7.2 第三方数据 / Third-party data

- AAM 原始数据（`.arff`）为第三方数据集，**不包含在本仓库中**。
- 使用者需自行从官方来源获取，并遵守原数据许可条款。

---

## 8. 源文件引用出处说明 / Source Attribution

### 8.1 数据集引用 / Dataset citation

- Ostermann, F., & Vatolkin, I. (2022). *AAM: Artificial Audio Multitracks Dataset (v1.1.0)* [Data set]. Zenodo. https://doi.org/10.5281/zenodo.5794629

### 8.2 本项目来源说明 / Project provenance

- 本仓库脚本与结果由论文工程中的 `aam_to_csv.py` 及其输出整理而来，保留核心处理逻辑与字段结构，用于教学与研究复现。

---

## 9. 快速复现建议 / Repro Tips

- 先跑小范围：`--id_start 0001 --id_end 0010` 验证流程；
- 再跑全量，保留 `--summary_json` 便于质量审计；
- 训练前建议先检查 `N` 与 `REST` 比例是否符合预期。
