import json, pathlib

# Old dialect label sample
p = pathlib.Path(r"C:\Users\User\Desktop\LEG\KT\LEG\data\voice\Old\02.라벨링데이터\01. 강원도\01. 1인발화 따라말하기\st_set2_collectorgw185_speakergw1744_63_9.json")
d = json.loads(p.read_text(encoding="utf-8"))

print("=== script.value (Ground Truth) ===")
print(d["script"]["value"])
print()
print("=== transcription segments (처음 5개) ===")
for seg in d["transcription"]["segments"][:5]:
    dialect = seg.get("dialect", "")
    standard = seg.get("standard", "")
    pron = seg.get("pronunciation", "")
    print(f"  dialect: {dialect!r:15s}  standard: {standard!r:15s}  pronunciation: {pron!r}")
print(f"  ... 총 {len(d['transcription']['segments'])}개 세그먼트")
print()
print("=== 전체 dialect 이어붙이기 ===")
full = " ".join(seg["dialect"] for seg in d["transcription"]["segments"] if seg.get("dialect"))
print(full)
