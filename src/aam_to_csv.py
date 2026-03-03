"""
AAM ARFF -> CSV 转换脚本 / AAM ARFF-to-CSV Converter

【用途 / Purpose】
1) 读取 AAM 三文件一组标注（segments/onsets/beatinfo）。
2) 以 group_id（如 0001）为单位完成解析与对齐，输出训练用 CSV。
3) 默认执行调性归一化（大调->C、小调->Am），并可输出 summary JSON。

【输出兼容性 / Output Compatibility】
输出字段与本项目 `train_lstm.py` 训练流程保持兼容。

【实现说明 / Notes】
- 本脚本尽量保持“可追溯、可复现、可调试”：核心步骤均可从输入文件定位到输出字段。
- 解析器采用手工 ARFF 读取逻辑，以增强对 AAM 文件细节（例如缺失 @DATA）的容错能力。
"""

import argparse
import csv
import json
import os
import re
import statistics
import time
from bisect import bisect_left

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from music21 import chord as m21_chord, key as m21_key, note as m21_note, roman as m21_roman


# 数值比较容差：用于浮点时间比较，避免 0.30000000004 之类误差影响分段判断。
EPS = 1e-9


# -----------------------------
# 数据结构定义（输入解析后的中间结构）
# -----------------------------
@dataclass
class SegmentEntry:
    start: float
    key_name: str
    instruments: List[str]
    generators: List[str]
    melody_instrument: Optional[str]


@dataclass
class BeatEntry:
    start: float
    bar: int
    quarter: int
    chord_name: str


@dataclass
class OnsetsTable:
    times: List[float]
    instruments: List[str]
    inst_index: Dict[str, int]
    cells: List[List[str]]


@dataclass
class GroupPaths:
    group_id: str
    segments_path: str
    onsets_path: str
    beatinfo_path: str


ROOT_TO_PC = {
    'C': 0,
    'B#': 0,
    'C#': 1,
    'Db': 1,
    'D': 2,
    'D#': 3,
    'Eb': 3,
    'E': 4,
    'Fb': 4,
    'E#': 5,
    'F': 5,
    'F#': 6,
    'Gb': 6,
    'G': 7,
    'G#': 8,
    'Ab': 8,
    'A': 9,
    'A#': 10,
    'Bb': 10,
    'B': 11,
    'Cb': 11,
}


def _safe_float(value: float) -> float:
    return float(round(value, 6))


def _normalize_semitones(shift: int) -> int:
    while shift > 6:
        shift -= 12
    while shift < -6:
        shift += 12
    return shift


def _normalized_key_and_shift(local_key: m21_key.Key) -> Tuple[str, int]:
    target_pc = 9 if local_key.mode == 'minor' else 0
    tonic_pc = local_key.tonic.pitchClass
    shift = _normalize_semitones(target_pc - tonic_pc)
    normalized_key = 'Am' if local_key.mode == 'minor' else 'C'
    return normalized_key, shift


def _key_to_string(local_key: m21_key.Key) -> str:
    return local_key.tonic.name + ('m' if local_key.mode == 'minor' else '')


def _pitch_name_from_midi(midi: int) -> str:
    return m21_note.Note(midi).pitch.nameWithOctave


def _parse_arff_manual(file_path: str) -> Tuple[List[str], List[List[str]]]:
    headers: List[str] = []
    rows: List[List[str]] = []
    in_data = False
    saw_attribute = False
    with open(file_path, 'r', encoding='utf-8') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('%'):
                continue
            lower = line.lower()
            if lower.startswith('@attribute'):
                saw_attribute = True
                m = re.match(r"@attribute\s+'([^']+)'\s+.+$", line, flags=re.IGNORECASE)
                if m:
                    headers.append(m.group(1).strip())
                else:
                    parts = line.split(None, 2)
                    if len(parts) >= 3:
                        headers.append(parts[1].strip("'\""))
                continue

            if lower.startswith('@data'):
                in_data = True
                continue

            if lower.startswith('@'):
                continue

            # 部分 AAM 文件无 @DATA，属性定义结束后直接进入数据区。
            if in_data or saw_attribute:
                parsed = next(csv.reader([line], delimiter=',', quotechar="'", skipinitialspace=True))
                rows.append([x.strip() for x in parsed])

    return headers, rows



def _parse_bracket_list(text: str) -> List[str]:
    s = text.strip().strip("'\"")
    if not s.startswith('[') or not s.endswith(']'):
        return []
    body = s[1:-1].strip()
    if not body:
        return []
    return [item.strip() for item in body.split(',') if item.strip()]


def _parse_pitch_events(cell: str) -> List[Tuple[int, bool]]:
    s = cell.strip().strip("'\"")
    if not s.startswith('[') or not s.endswith(']'):
        return []
    body = s[1:-1].strip()
    if not body:
        return []
    out: List[Tuple[int, bool]] = []
    for tok in body.split(','):
        t = tok.strip()
        if not t:
            continue
        is_onset = t.startswith('+')
        if is_onset:
            t = t[1:]
        try:
            out.append((int(t), is_onset))
        except ValueError:
            continue
    return out


def _pick_pitch_from_cell(cell: str) -> Optional[int]:
    events = _parse_pitch_events(cell)
    if not events:
        return None
    onset_pitches = [p for p, is_onset in events if is_onset]
    if onset_pitches:
        return max(onset_pitches)
    return max(p for p, _ in events)


def _key_from_aam_text(key_text: str) -> m21_key.Key:
    s = key_text.strip().strip("'\"")
    if not s:
        return m21_key.Key('C')

    m = re.match(r'^([A-Ga-g])([#b]?)(maj|min)$', s)
    if not m:
        return m21_key.Key('C')

    tonic = m.group(1).upper() + m.group(2)
    mode = 'minor' if m.group(3).lower() == 'min' else 'major'
    return m21_key.Key(tonic, mode)


def _roman_degree_from_label(label: str) -> str:
    roman = ''
    for ch in label:
        if ch in 'ivIV':
            roman += ch
        else:
            break
    return roman


def _has_interval(ch: m21_chord.Chord, semitones: int) -> bool:
    root = ch.root()
    if root is None:
        return False
    for p in ch.pitches:
        interval = (p.midi - root.midi) % 12
        if interval == semitones:
            return True
    return False


def _is_sus4(ch: m21_chord.Chord) -> bool:
    return _has_interval(ch, 5) and not _has_interval(ch, 4) and not _has_interval(ch, 3)


def _is_sus2(ch: m21_chord.Chord) -> bool:
    return _has_interval(ch, 2) and not _has_interval(ch, 4) and not _has_interval(ch, 3)


def _chord_to_label(ch: m21_chord.Chord, local_key: m21_key.Key) -> str:
    try:
        rn = m21_roman.romanNumeralFromChord(ch, local_key)
    except Exception:
        return 'N'

    if rn is None:
        return 'N'

    degree = rn.romanNumeral
    quality = rn.quality
    suffix = ''

    if quality == 'diminished':
        suffix = 'dim'
    elif quality == 'augmented':
        suffix = 'aug'
    elif quality == 'half-diminished':
        suffix = 'hdim'

    inversion = rn.inversion()
    if rn.isTriad():
        inv_map = {0: '', 1: '6', 2: '64'}
        inv_suffix = inv_map.get(inversion, '')
    else:
        inv_map = {0: '7', 1: '65', 2: '43', 3: '42'}
        inv_suffix = inv_map.get(inversion, '7')

    sus = ''
    if _is_sus4(ch):
        sus = 'sus'
    elif _is_sus2(ch):
        sus = 'sus2'

    if rn.isSeventh() and inv_suffix == '7':
        label = f"{degree}{suffix}{sus}7"
    else:
        label = f"{degree}{suffix}{sus}{inv_suffix}"

    if not _roman_degree_from_label(label):
        return 'N'
    return label


def _aam_chord_name_to_chord(chord_name: str) -> Optional[m21_chord.Chord]:
    s = chord_name.strip().strip("'\"")
    if not s:
        return None
    if s.upper() in {'N', 'NC', 'NO_CHORD'}:
        return None

    m = re.match(r'^([A-Ga-g])([#b]?)(.*)$', s)
    if not m:
        return None

    root = m.group(1).upper() + m.group(2)
    tail = m.group(3).strip().lower().replace(' ', '')
    if root not in ROOT_TO_PC:
        return None

    intervals = [0, 4, 7]
    if 'sus2' in tail:
        intervals = [0, 2, 7]
    elif 'sus4' in tail or 'sus' in tail:
        intervals = [0, 5, 7]
    elif 'm7b5' in tail or 'hdim' in tail:
        intervals = [0, 3, 6, 10]
    elif 'dim7' in tail:
        intervals = [0, 3, 6, 9]
    elif tail.startswith('dim'):
        intervals = [0, 3, 6]
    elif tail.startswith('aug') or '+' in tail:
        intervals = [0, 4, 8]
    elif 'maj9' in tail:
        intervals = [0, 4, 7, 11, 14]
    elif 'add9' in tail:
        intervals = [0, 4, 7, 14]
    elif 'maj7' in tail:
        intervals = [0, 4, 7, 11]
    elif 'min9' in tail or re.match(r'^m(?!aj).*9', tail):
        intervals = [0, 3, 7, 10, 14]
    elif 'min7' in tail or re.match(r'^m(?!aj).*7', tail):
        intervals = [0, 3, 7, 10]
    elif '7' in tail:
        intervals = [0, 4, 7, 10]
    elif tail.startswith('min') or (tail.startswith('m') and not tail.startswith('maj')):
        intervals = [0, 3, 7]

    base = 60 + ROOT_TO_PC[root]
    try:
        return m21_chord.Chord([base + iv for iv in intervals])
    except Exception:
        return None



def _aam_chord_to_label(chord_name: str, local_key: m21_key.Key) -> str:
    ch = _aam_chord_name_to_chord(chord_name)
    if ch is None:
        return 'N'
    return _chord_to_label(ch, local_key)


def _parse_segments(file_path: str) -> List[SegmentEntry]:
    headers, rows = _parse_arff_manual(file_path)
    idx = {name: i for i, name in enumerate(headers)}

    req = ['Start time in seconds', 'Key', 'Instruments', 'Generator']
    for col in req:
        if col not in idx:
            raise ValueError(f'{os.path.basename(file_path)} missing column: {col}')

    segments: List[SegmentEntry] = []
    for row in rows:
        start = float(row[idx['Start time in seconds']])
        key_name = row[idx['Key']].strip().strip("'\"")
        instruments = _parse_bracket_list(row[idx['Instruments']])
        generators = _parse_bracket_list(row[idx['Generator']])

        melody_inst: Optional[str] = None
        for i, g in enumerate(generators):
            if 'melody' in g.lower() and i < len(instruments):
                melody_inst = instruments[i]
                break

        segments.append(
            SegmentEntry(
                start=start,
                key_name=key_name,
                instruments=instruments,
                generators=generators,
                melody_instrument=melody_inst,
            )
        )

    segments.sort(key=lambda x: x.start)
    return segments


def _parse_beatinfo(file_path: str) -> List[BeatEntry]:
    headers, rows = _parse_arff_manual(file_path)
    idx = {name: i for i, name in enumerate(headers)}

    req = ['Start time in seconds', 'Bar count', 'Quarter count', 'Chord name']
    for col in req:
        if col not in idx:
            raise ValueError(f'{os.path.basename(file_path)} missing column: {col}')

    beats: List[BeatEntry] = []
    for row in rows:
        beats.append(
            BeatEntry(
                start=float(row[idx['Start time in seconds']]),
                bar=int(float(row[idx['Bar count']])),
                quarter=int(float(row[idx['Quarter count']])),
                chord_name=row[idx['Chord name']].strip().strip("'\""),
            )
        )

    beats.sort(key=lambda x: x.start)
    return beats


def _parse_onsets(file_path: str) -> OnsetsTable:
    headers, rows = _parse_arff_manual(file_path)
    if not headers:
        raise ValueError(f'{os.path.basename(file_path)} has no headers')

    time_idx = 0
    instruments: List[str] = []
    inst_cols: List[int] = []

    for i, h in enumerate(headers):
        if h.lower() == 'onset time in seconds':
            time_idx = i
            continue
        m = re.match(r'^Onset events of (.+)$', h)
        if m:
            instruments.append(m.group(1).strip())
            inst_cols.append(i)

    if not instruments:
        raise ValueError(f'{os.path.basename(file_path)} has no onset instrument columns')

    times: List[float] = []
    cells: List[List[str]] = []
    for row in rows:
        times.append(float(row[time_idx]))
        cells.append([row[c] for c in inst_cols])

    return OnsetsTable(
        times=times,
        instruments=instruments,
        inst_index={name: i for i, name in enumerate(instruments)},
        cells=cells,
    )


def _pick_fallback_melody_instrument(onsets: OnsetsTable) -> Optional[str]:
    stats: Dict[str, Tuple[float, int]] = {}
    for inst in onsets.instruments:
        lower = inst.lower()
        if 'drum' in lower or 'bass' in lower:
            continue
        idx = onsets.inst_index[inst]
        total = 0.0
        cnt = 0
        for row in onsets.cells:
            p = _pick_pitch_from_cell(row[idx])
            if p is None:
                continue
            total += float(p)
            cnt += 1
        if cnt > 0:
            stats[inst] = (total / cnt, cnt)

    if not stats:
        return None

    return max(stats.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]


def _nearest_time_index(times: Sequence[float], target: float) -> int:
    if not times:
        return 0
    pos = bisect_left(times, target)
    if pos <= 0:
        return 0
    if pos >= len(times):
        return len(times) - 1
    left = pos - 1
    if abs(times[pos] - target) < abs(target - times[left]):
        return pos
    return left


def _build_groups(aam_dir: str, allow_txt: bool) -> List[GroupPaths]:
    seg_re = re.compile(r'^(\d{4})_segments\.(arff|txt)$', flags=re.IGNORECASE)
    on_re = re.compile(r'^(\d{4})_onsets\.(arff|txt)$', flags=re.IGNORECASE)
    beat_re = re.compile(r'^(\d{4})_beatinfo\.(arff|txt)$', flags=re.IGNORECASE)

    segments: Dict[str, str] = {}
    onsets: Dict[str, str] = {}
    beatinfo: Dict[str, str] = {}

    for name in os.listdir(aam_dir):
        full = os.path.join(aam_dir, name)
        if not os.path.isfile(full):
            continue

        m = seg_re.match(name)
        if m and (allow_txt or m.group(2).lower() == 'arff'):
            gid = m.group(1)
            prev = segments.get(gid)
            if prev is None or prev.lower().endswith('.txt'):
                segments[gid] = full
            continue

        m = on_re.match(name)
        if m and (allow_txt or m.group(2).lower() == 'arff'):
            gid = m.group(1)
            prev = onsets.get(gid)
            if prev is None or prev.lower().endswith('.txt'):
                onsets[gid] = full
            continue

        m = beat_re.match(name)
        if m and (allow_txt or m.group(2).lower() == 'arff'):
            gid = m.group(1)
            prev = beatinfo.get(gid)
            if prev is None or prev.lower().endswith('.txt'):
                beatinfo[gid] = full

    ids = sorted(set(segments) | set(onsets) | set(beatinfo))
    groups: List[GroupPaths] = []
    for gid in ids:
        if gid in segments and gid in onsets and gid in beatinfo:
            groups.append(
                GroupPaths(
                    group_id=gid,
                    segments_path=segments[gid],
                    onsets_path=onsets[gid],
                    beatinfo_path=beatinfo[gid],
                )
            )
    return groups


def _convert_group(
    group: GroupPaths,
    normalize_key: bool,
) -> Tuple[List[Dict[str, str]], Dict[str, object]]:
    """将单个 group（三文件）转换为 CSV 行，并返回该组 summary。"""
    segments = _parse_segments(group.segments_path)
    beats = _parse_beatinfo(group.beatinfo_path)
    onsets = _parse_onsets(group.onsets_path)

    if not beats:
        return [], {'group_id': group.group_id, 'status': 'skip', 'reason': 'empty beatinfo'}

    fallback_inst = _pick_fallback_melody_instrument(onsets)

    seg_idx = 0
    starts = [b.start for b in beats]
    diffs = [starts[i + 1] - starts[i] for i in range(len(starts) - 1) if starts[i + 1] > starts[i] + EPS]
    sec_per_beat = statistics.median(diffs) if diffs else 1.0
    if sec_per_beat <= EPS:
        sec_per_beat = 1.0

    rows: List[Dict[str, str]] = []
    n_count = 0
    rest_count = 0
    melody_from_generator = 0
    melody_from_fallback = 0

    for i, beat in enumerate(beats):
        while seg_idx + 1 < len(segments) and segments[seg_idx + 1].start <= beat.start + EPS:
            seg_idx += 1

        seg = segments[seg_idx] if segments else SegmentEntry(0.0, 'Cmaj', [], [], None)
        local_key = _key_from_aam_text(seg.key_name)
        key_original = _key_to_string(local_key)
        if normalize_key:
            key_normalized, shift = _normalized_key_and_shift(local_key)
        else:
            key_normalized, shift = key_original, 0

        melody_inst = seg.melody_instrument
        source = 'generator'
        if not melody_inst:
            melody_inst = fallback_inst
            source = 'fallback'

        midi_original = -1
        if melody_inst and melody_inst in onsets.inst_index and onsets.times:
            row_idx = _nearest_time_index(onsets.times, beat.start)
            inst_idx = onsets.inst_index[melody_inst]
            picked = _pick_pitch_from_cell(onsets.cells[row_idx][inst_idx])
            if picked is not None:
                midi_original = picked

        if midi_original >= 0:
            melody_midi_norm = midi_original + shift
            melody_pitch_original = _pitch_name_from_midi(midi_original)
            melody_pitch_norm = _pitch_name_from_midi(melody_midi_norm)
            melody_midi_str = str(melody_midi_norm)
        else:
            rest_count += 1
            melody_pitch_original = 'REST'
            melody_pitch_norm = 'REST'
            melody_midi_str = '-1'

        if source == 'generator':
            melody_from_generator += 1
        else:
            melody_from_fallback += 1

        chord_label = _aam_chord_to_label(beat.chord_name, local_key)
        if chord_label == 'N':
            n_count += 1

        if i + 1 < len(beats):
            dur_q = (beats[i + 1].start - beat.start) / sec_per_beat
            if dur_q <= EPS:
                dur_q = 1.0
        else:
            dur_q = 1.0

        rows.append({
            'piece_id': group.group_id,
            'offset': str(_safe_float(float(i))),
            'duration': str(_safe_float(float(dur_q))),
            'measure': str(i // 4 + 1),
            'key': key_normalized,
            'key_original': key_original,
            'key_normalized': key_normalized,
            'transpose_semitones': str(shift),
            'melody_pitch': melody_pitch_norm,
            'melody_midi': melody_midi_str,
            'melody_pitch_original': melody_pitch_original,
            'melody_midi_original': str(midi_original),
            'chord_label': chord_label,
            'chord_label_raw': beat.chord_name,
        })

    total_rows = len(rows)
    summary = {
        'group_id': group.group_id,
        'status': 'ok',
        'rows': total_rows,
        'rest_rows': rest_count,
        'n_rows': n_count,
        'n_ratio': round((n_count / total_rows), 6) if total_rows > 0 else 0.0,
        'melody_source_generator_rows': melody_from_generator,
        'melody_source_fallback_rows': melody_from_fallback,
        'fallback_instrument': fallback_inst,
    }

    return rows, summary


def main():
    # CLI 入口：建议先用小范围 id_start/id_end 验证，再跑全量数据。
    parser = argparse.ArgumentParser(description='Convert AAM grouped ARFF annotations to training CSV.')
    parser.add_argument('--aam_dir', required=True, help='Directory containing grouped AAM annotations (xxxx_segments/onsets/beatinfo).')
    parser.add_argument('--output_csv', required=True, help='Output CSV path compatible with train_lstm.py.')
    parser.add_argument('--summary_json', default='', help='Optional summary JSON output path.')
    parser.add_argument('--id_start', default='0001', help='Start group id, inclusive (e.g., 0001).')
    parser.add_argument('--id_end', default='9999', help='End group id, inclusive (e.g., 1000).')
    parser.add_argument('--no_normalize_key', action='store_true', help='Disable key normalization to C/Am.')
    parser.add_argument('--allow_txt', action='store_true', help='Allow .txt files with ARFF content in addition to .arff.')
    parser.add_argument('--log_every', type=int, default=50, help='Print progress every N groups.')


    args = parser.parse_args()

    if not os.path.isdir(args.aam_dir):
        raise FileNotFoundError(f'AAM directory not found: {args.aam_dir}')

    groups = _build_groups(args.aam_dir, allow_txt=args.allow_txt)
    if not groups:
        raise ValueError('No complete AAM groups found.')

    groups = [g for g in groups if args.id_start <= g.group_id <= args.id_end]
    if not groups:
        raise ValueError(f'No groups in requested id range: {args.id_start}..{args.id_end}')

    # rows_all: 最终写入 CSV 的全量记录。
    # summaries: 每个 group 的统计信息（可选写入 summary_json）。
    # failed: 失败 group 的错误信息，便于后续排查。
    rows_all: List[Dict[str, str]] = []
    summaries: List[Dict[str, object]] = []
    failed: List[Dict[str, str]] = []

    total = len(groups)
    total_start = time.perf_counter()
    for idx, group in enumerate(groups, start=1):
        group_start = time.perf_counter()
        try:
            rows, summary = _convert_group(group, normalize_key=not args.no_normalize_key)
            group_elapsed = time.perf_counter() - group_start
            summary['elapsed_sec'] = round(group_elapsed, 6)

            rows_all.extend(rows)

            summaries.append(summary)
            print(f"[TIME] group {group.group_id} processed in {group_elapsed:.3f}s, rows={len(rows)}")
        except Exception as exc:

            group_elapsed = time.perf_counter() - group_start
            failed.append({'group_id': group.group_id, 'error': str(exc), 'elapsed_sec': round(group_elapsed, 6)})
            print(f"[TIME] group {group.group_id} failed in {group_elapsed:.3f}s: {exc}")

        if args.log_every > 0 and (idx % args.log_every == 0 or idx == total):
            print(f"[INFO] Processed {idx}/{total} groups, rows={len(rows_all)}, failed={len(failed)}")


    # -----------------------------
    # 写出主结果 CSV
    # -----------------------------
    os.makedirs(os.path.dirname(args.output_csv), exist_ok=True)
    with open(args.output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                'piece_id',
                'offset',
                'duration',
                'measure',
                'key',
                'key_original',
                'key_normalized',
                'transpose_semitones',
                'melody_pitch',
                'melody_midi',
                'melody_pitch_original',
                'melody_midi_original',
                'chord_label',
                'chord_label_raw',
            ],

        )
        writer.writeheader()
        writer.writerows(rows_all)

    rest_total = sum(int(s.get('rest_rows', 0)) for s in summaries)
    n_total = sum(int(s.get('n_rows', 0)) for s in summaries)

    total_elapsed = time.perf_counter() - total_start
    success_elapsed_values = [float(s.get('elapsed_sec', 0.0)) for s in summaries]
    avg_group_elapsed = (sum(success_elapsed_values) / len(success_elapsed_values)) if success_elapsed_values else 0.0

    # 汇总级报告（全局统计）
    report = {
        'aam_dir': args.aam_dir,
        'id_start': args.id_start,
        'id_end': args.id_end,
        'normalize_key': not args.no_normalize_key,
        'total_groups_requested': total,

        'success_groups': len(summaries),
        'failed_groups': len(failed),
        'total_rows': len(rows_all),
        'rest_rows': rest_total,
        'n_rows': n_total,
        'total_elapsed_sec': round(total_elapsed, 6),
        'avg_success_group_elapsed_sec': round(avg_group_elapsed, 6),
        'failed': failed,
    }


    print(
        f"[INFO] Saved {len(rows_all)} rows from {len(summaries)}/{total} groups "
        f"to {args.output_csv} (failed={len(failed)}, REST={rest_total}, N={n_total}, total_time={total_elapsed:.3f}s)"
    )


    if args.summary_json:
        os.makedirs(os.path.dirname(args.summary_json), exist_ok=True)
        with open(args.summary_json, 'w', encoding='utf-8') as f:
            json.dump({'report': report, 'groups': summaries}, f, ensure_ascii=False, indent=2)
        print(f"[INFO] Saved summary to {args.summary_json}")


if __name__ == '__main__':
    main()
