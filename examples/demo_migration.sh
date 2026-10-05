#!/usr/bin/env bash
# 标注迁移端到端示例：
#   上传 1 份源图（40x30）与 2 份同版式不同尺寸目标图（100x80、20x10）
#   -> 源上标注 2 个敏感矩形 -> 调 POST /annotation-migrations 整体迁移
#   -> 各目标标注被整体替换为按旋正后宽高比例缩放的矩形（修订号各加一），
#      原因与顺序保留；随后可直接发布，涂黑位置与映射矩形一致
#   -> 演示各类整批失败（全部目标不变）：数量越界、目标重复、源作为目标、
#      目标缺失、修订过期、源标注为空
# 依赖：curl、python3（仅用于解析 JSON、生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (40, 30), (30, 90, 160)).save("/tmp/mig_src.png")
Image.new("RGB", (100, 80), (160, 90, 30)).save("/tmp/mig_t1.png")
Image.new("RGB", (20, 10), (90, 30, 160)).save("/tmp/mig_t2.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 上传源图（40x30）与两份目标图（100x80、20x10）=="
SRC=$(upload /tmp/mig_src.png)
T1=$(upload /tmp/mig_t1.png)
T2=$(upload /tmp/mig_t2.png)
echo "SRC=$SRC"; echo "T1=$T1"; echo "T2=$T2"

echo
echo "== 2. 在源上标注两个敏感矩形（修订 0 -> 1）=="
curl -sS -X PUT "$BASE/documents/$SRC/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[
        {"x":5,"y":5,"w":10,"h":10,"reason":"人脸"},
        {"x":39,"y":29,"w":1,"h":1,"reason":"右下角工号"}
      ]}'

echo
echo "== 3. 迁移：源当前标注按旋正后宽高比例缩放到 T1、T2（整批原子）=="
curl -sS -X POST "$BASE/annotation-migrations" -H 'Content-Type: application/json' \
  -d "{
    \"source\": {\"doc_id\": \"$SRC\", \"expected_revision\": 1},
    \"targets\": [
      {\"doc_id\": \"$T1\", \"expected_revision\": 0},
      {\"doc_id\": \"$T2\", \"expected_revision\": 0}
    ]
  }" | python3 -m json.tool
# T1(100x80)：人脸 -> x=floor(5*100/40)=12, y=floor(5*80/30)=13,
#             w=ceil(15*100/40)-12=26, h=ceil(15*80/30)-13=27
# T2(20x10) ：人脸 -> x=2, y=1, w=6, h=4；右下角工号被限制在图内

echo
echo "== 4. 目标当前标注已整体替换（修订号各加一），可直接发布 =="
curl -sS "$BASE/documents/$T1/annotations" | python3 -m json.tool
curl -sS -X POST "$BASE/documents/$T1/publish" | python3 -m json.tool

echo
echo "== 5. 源标注与既有发布版不受迁移影响 =="
curl -sS "$BASE/documents/$SRC/annotations" | python3 -m json.tool

echo
echo "== 6. 目标修订过期 -> 409 stale_revision（附 position，全部目标不变）=="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/annotation-migrations" \
  -H 'Content-Type: application/json' \
  -d "{
    \"source\": {\"doc_id\": \"$SRC\", \"expected_revision\": 1},
    \"targets\": [
      {\"doc_id\": \"$T1\", \"expected_revision\": 1},
      {\"doc_id\": \"$T2\", \"expected_revision\": 0}
    ]
  }"

echo
echo "== 7. 源作为目标 -> 400 source_is_target =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/annotation-migrations" \
  -H 'Content-Type: application/json' \
  -d "{
    \"source\": {\"doc_id\": \"$SRC\", \"expected_revision\": 1},
    \"targets\": [
      {\"doc_id\": \"$T1\", \"expected_revision\": 1},
      {\"doc_id\": \"$SRC\", \"expected_revision\": 1}
    ]
  }"

echo
echo "== 8. 目标重复 -> 400 duplicate_document_in_migration =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/annotation-migrations" \
  -H 'Content-Type: application/json' \
  -d "{
    \"source\": {\"doc_id\": \"$SRC\", \"expected_revision\": 1},
    \"targets\": [
      {\"doc_id\": \"$T1\", \"expected_revision\": 1},
      {\"doc_id\": \"$T1\", \"expected_revision\": 1}
    ]
  }"

echo
echo "== 9. 源标注为空 -> 400 empty_source_annotations =="
EMPTY=$(upload /tmp/mig_t2.png)
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/annotation-migrations" \
  -H 'Content-Type: application/json' \
  -d "{
    \"source\": {\"doc_id\": \"$EMPTY\", \"expected_revision\": 0},
    \"targets\": [{\"doc_id\": \"$T2\", \"expected_revision\": 1}]
  }"

echo
echo "== 10. 未知目标 -> 404 document_not_found（附 position）=="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/annotation-migrations" \
  -H 'Content-Type: application/json' \
  -d "{
    \"source\": {\"doc_id\": \"$SRC\", \"expected_revision\": 1},
    \"targets\": [
      {\"doc_id\": \"$T1\", \"expected_revision\": 1},
      {\"doc_id\": \"00000000000000000000000000000000\", \"expected_revision\": 0}
    ]
  }"

echo
echo "== 11. 修正修订号后重试成功（失败未占用任何目标修订号）=="
curl -sS -X POST "$BASE/annotation-migrations" -H 'Content-Type: application/json' \
  -d "{
    \"source\": {\"doc_id\": \"$SRC\", \"expected_revision\": 1},
    \"targets\": [
      {\"doc_id\": \"$T1\", \"expected_revision\": 1},
      {\"doc_id\": \"$T2\", \"expected_revision\": 1}
    ]
  }" | python3 -m json.tool
