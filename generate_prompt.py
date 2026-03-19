#!/usr/bin/env python3
"""
vault_work의 md 파일에서 도메인 용어를 추출하여
Whisper STT initial_prompt 파일을 생성합니다.

Usage:
    python generate_prompt.py [--vault /path/to/vault] [--output /path/to/output.txt]
"""

import argparse
import json
import re
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
import openai

DEFAULT_VAULT = Path("/Users/user/Downloads/vault_work")
DEFAULT_OUTPUT = Path(__file__).parent / "initial_prompt.txt"
FILTER_PROMPT_PATH = Path(__file__).parent / "filter_prompt.txt"
MODEL = "gpt-5-mini"
BATCH_SIZE = 2000       # 1단계: 유지/제거 분류 배치 크기
REASON_BATCH_SIZE = 1000  # 2단계: 제거 이유 요청 배치 크기

# 언어적 잔재만 제거 (조사·어미·마크다운 artifacts)
# 보편적 단어 판단은 GPT가 담당
JUNK = {
    # 한국어 조사/어미
    "의", "을", "를", "이", "가", "은", "는", "에", "에서", "으로", "로", "와", "과",
    "도", "만", "까지", "부터", "이나", "이라", "라고", "라는", "에게", "한테", "께",
    "하다", "있다", "없다", "되다", "이다", "같다", "하는", "있는", "없는", "되는",
    "되었다", "했다", "한다", "할", "될", "위해", "위한", "통해", "따른", "따라",
    # YAML/마크다운 잔재
    "null", "true", "false", "title", "date", "allday", "completed", "description",
    "tags", "aliases", "type", "created", "modified", "status", "enddate",
    # URL 파편
    "docs", "edit", "gid", "viewpage", "pageid", "spreadsheets", "https", "http",
}


def clean_text(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text) # 제어문자 제거 (\x08 백스페이스 포함, \n\t\r 제외)
    text = re.sub(r"^---.*?---\s*", "", text, flags=re.DOTALL)     # YAML frontmatter
    text = re.sub(r"https?://\S+", "", text)                        # URLs
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)                     # 이미지
    text = re.sub(r"\[.*?\]\(.*?\)", "", text)                      # 링크
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)          # 코드 블록
    text = re.sub(r"`[^`]+`", "", text)                             # 인라인 코드
    text = re.sub(r"[#*>|~_!]", " ", text)                         # 마크다운 문자
    return text


# 조사 + 어미 (길이 긴 것부터 — 짧은 것 먼저 매칭되면 잘못 자름 방지)
_ENDINGS = [
    # 어미 (긴 것)
    "이므로", "이라서", "이어서", "이지만", "이라는", "이라고", "이라면", "이라며",
    "하므로", "하지만", "하면서", "하는데", "하니까", "하여서", "하였고", "하였다", "하였으며",
    "되므로", "되지만", "되면서", "되는데", "되니까", "되었고", "되었다", "되었으며",
    "으므로", "으로써", "으로서",
    "습니다", "ㅂ니다", "았다", "었다",
    # 어미 (짧은 것)
    "하고", "하며", "하여", "하면", "하던", "하든",
    "되고", "되며", "되어", "되면",
    "이고", "이며", "이나",
    "지만", "면서", "는데", "니까",
    # 조사
    "에서부터", "으로부터", "로부터",
    "에서", "으로", "에게", "한테", "까지", "부터", "이라", "하고", "이나",
    "를", "을", "는", "은", "가", "이", "도", "만", "의", "와", "과", "로", "에",
]


def strip_endings(term: str) -> str:
    for e in _ENDINGS:
        if term.endswith(e) and len(term) - len(e) >= 2:
            return term[: -len(e)]
    return term


def extract_terms(text: str) -> set:
    text = clean_text(text)
    tokens = re.split(r"[\s\n\r\t,.:;()\-\'\"@/\\+=\[\]{}?&]+", text)
    terms = set()
    for t in tokens:
        t = strip_endings(t.strip())
        if len(t) < 2:
            continue
        if re.match(r"^\d+$", t):                      # 순수 숫자
            continue
        if re.match(r"^\d{4}-\d{2}", t):               # 날짜
            continue
        if re.match(r"^[^\w가-힣]+$", t):              # 특수문자·공백만으로 구성
            continue
        if not re.search(r"[a-zA-Z가-힣]", t):         # 한글·영문 최소 1자 없으면 제외
            continue
        if t.lower() in JUNK:
            continue
        terms.add(t)
    return terms


def sanitize(term: str) -> str:
    """JSON 파싱을 깨뜨리는 제어문자 제거"""
    return re.sub(r"[\x00-\x1f\x7f]", "", term).strip()


# 1단계: 유지/제거 분류만 (컴팩트 출력)
_CLASSIFY_INSTRUCTION = """
출력 형식: 두 줄로만 응답하세요. 설명 없이 아래 형식만 출력하세요.
kept: 유지할 용어들을 쉼표로 구분
removed: 제거할 용어들을 쉼표로 구분
"""

# 2단계: 제거 이유 요청
_REASON_INSTRUCTION = """
입력된 단어들은 도메인 특화 용어 필터링에서 제거된 단어들입니다.
각 단어가 왜 제거되었는지 이유를 JSON 배열로 출력하세요. JSON만 출력하세요.
[
  {"term": "단어1", "reason": "제거 이유"},
  {"term": "단어2", "reason": "제거 이유"}
]
"""


def classify_batch(client: openai.OpenAI, terms: list, system_prompt: str) -> tuple[list, list]:
    """1단계: 유지/제거 분류만 (이유 없음, 컴팩트 출력)"""
    clean_terms = [s for t in terms if (s := sanitize(t))]
    if not clean_terms:
        return [], []
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt + _CLASSIFY_INSTRUCTION},
                {"role": "user", "content": ", ".join(clean_terms)},
            ],
        )
        raw = response.choices[0].message.content.strip()
        kept, removed_terms = [], []
        for line in raw.splitlines():
            line = line.strip()
            if line.lower().startswith("kept:"):
                kept = [t.strip() for t in line[5:].split(",") if t.strip()]
            elif line.lower().startswith("removed:"):
                removed_terms = [t.strip() for t in line[8:].split(",") if t.strip()]

        # GPT가 누락한 단어 → removed 처리
        responded = set(kept) | set(removed_terms)
        for t in clean_terms:
            if t not in responded:
                removed_terms.append(t)

        return kept, removed_terms
    except Exception as e:
        print(f"[경고] 분류 배치 오류, 스킵: {e}")
        return [], list(clean_terms)


def get_reasons(client: openai.OpenAI, batch: list) -> list:
    """2단계: 제거된 단어 배치에 대해 이유 요청 (스트리밍)"""
    try:
        stream = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": _REASON_INSTRUCTION},
                {"role": "user", "content": ", ".join(batch)},
            ],
            stream=True,
        )
        print("  응답 수신 중: ", end="", flush=True)
        raw_chunks = []
        char_count = 0
        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            raw_chunks.append(delta)
            # 응답 미리보기: 줄바꿈 제거 후 150자까지만 출력
            for ch in delta:
                if ch in ("\n", "\r"):
                    continue
                if char_count < 150:
                    print(ch, end="", flush=True)
                    char_count += 1
                elif char_count == 150:
                    print("...", flush=True)
                    char_count += 1  # 이후 출력 안 함

        if char_count <= 150:
            print()  # 줄바꿈

        raw = "".join(raw_chunks).strip()
        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            responded = {r.get("term", "").strip() for r in data}
            result = list(data)
            for t in batch:
                if t not in responded:
                    result.append({"term": t, "reason": "GPT 미응답"})
            return result
        else:
            return [{"term": t, "reason": "파싱 실패"} for t in batch]
    except Exception as e:
        print()
        return [{"term": t, "reason": f"오류: {e}"} for t in batch]


def main():
    parser = argparse.ArgumentParser(description="vault md 파일에서 STT initial_prompt 생성")
    parser.add_argument("--vault", default=str(DEFAULT_VAULT), help="Obsidian vault 경로")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="출력 파일 경로")
    args = parser.parse_args()

    vault = Path(args.vault)
    output = Path(args.output)

    env_path = Path.home() / "Library" / "Application Support" / "AutoMeetingNote" / ".env"
    load_dotenv(env_path)

    print(f"볼트 경로: {vault}")
    md_files = [f for f in vault.rglob("*.md") if ".obsidian" not in str(f)]
    print(f"MD 파일 수: {len(md_files)}")

    # 전체 고유 용어 수집 (빈도 무관)
    all_terms: set = set()
    for i, f in enumerate(md_files):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            all_terms.update(extract_terms(text))
        except Exception as e:
            print(f"  스킵: {f.name} ({e})")
        if (i + 1) % 100 == 0:
            print(f"  진행: {i + 1}/{len(md_files)}")

    unique_terms = sorted(all_terms)
    print(f"\n고유 용어 수: {len(unique_terms)}")

    # 전체 추출 용어 저장
    output.parent.mkdir(parents=True, exist_ok=True)
    all_terms_path = output.parent / "initial_prompt_all_terms.txt"
    all_terms_path.write_text("\n".join(unique_terms), encoding="utf-8")
    print(f"전체 용어 저장: {all_terms_path} ({len(unique_terms)}개)")

    # 필터 프롬프트 로드
    if not FILTER_PROMPT_PATH.exists():
        print(f"❌ 프롬프트 파일 없음: {FILTER_PROMPT_PATH}")
        return
    system_prompt = FILTER_PROMPT_PATH.read_text(encoding="utf-8").strip()
    print(f"프롬프트 파일: {FILTER_PROMPT_PATH}")

    client = openai.OpenAI()
    candidates_path = output.parent / "initial_prompt_candidates.txt"
    stage1_path = output.parent / "initial_prompt_stage1_classify.txt"
    stage2_path = output.parent / "initial_prompt_stage2_reasons.txt"
    checkpoint_path = output.parent / "checkpoint.json"

    # 체크포인트 로드
    ckpt = _load_checkpoint(checkpoint_path, unique_terms)
    selected: list = ckpt["kept"]
    all_removed_terms: list = ckpt["removed_terms"]
    all_removed: list = ckpt["removed_with_reasons"]
    stage1_done: bool = ckpt["stage1_done"]
    stage2_done: bool = ckpt["stage2_done"]

    if stage1_done:
        print(f"\n[1단계] 체크포인트 복원: 유지 {len(selected)}개 / 제거 {len(all_removed_terms)}개 (건너뜀)")
    else:
        # 이미 처리된 용어 수 계산해서 재개 지점 찾기
        already = len(selected) + len(all_removed_terms)
        resume_idx = (already // BATCH_SIZE) * BATCH_SIZE
        total_batches = (len(unique_terms) + BATCH_SIZE - 1) // BATCH_SIZE

        if resume_idx > 0:
            print(f"\n[1단계] {resume_idx}번째 용어부터 재개...")
        else:
            print(f"\n[1단계] 유지/제거 분류 ({total_batches}배치, 배치당 {BATCH_SIZE}개, 총 {len(unique_terms)}개)...")

        for i in range(resume_idx, len(unique_terms), BATCH_SIZE):
            batch = unique_terms[i : i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            print(f"  [{batch_num}/{total_batches}]", end=" ", flush=True)
            kept, removed_terms = classify_batch(client, batch, system_prompt)
            selected.extend(kept)
            all_removed_terms.extend(removed_terms)
            evaluated = len(selected) + len(all_removed_terms)
            print(f"유지 {len(kept)}개 / 제거 {len(removed_terms)}개 (누적: {evaluated}/{len(unique_terms)})")

            lines = ["=== 유지 ==="] + selected + ["", "=== 제거 ==="] + all_removed_terms
            stage1_path.write_text("\n".join(lines), encoding="utf-8")
            _save_checkpoint(checkpoint_path, unique_terms, selected, all_removed_terms, all_removed, stage1_done=False, stage2_done=False)

        stage1_done = True
        _save_checkpoint(checkpoint_path, unique_terms, selected, all_removed_terms, all_removed, stage1_done=True, stage2_done=False)
        print(f"\n분류 완료: 유지 {len(selected)}개 / 제거 {len(all_removed_terms)}개")
        print(f"1단계 저장: {stage1_path}")

    if stage2_done:
        print(f"\n[2단계] 체크포인트 복원: 이유 {len(all_removed)}개 (건너뜀)")
    else:
        already_reasoned = len(all_removed)
        resume_idx = (already_reasoned // REASON_BATCH_SIZE) * REASON_BATCH_SIZE
        reason_batches = (len(all_removed_terms) + REASON_BATCH_SIZE - 1) // REASON_BATCH_SIZE

        if resume_idx > 0:
            print(f"\n[2단계] {resume_idx}번째 제거 용어부터 재개...")
        else:
            print(f"\n[2단계] 제거 이유 요청 ({reason_batches}배치, 배치당 {REASON_BATCH_SIZE}개)...")

        for i in range(resume_idx, len(all_removed_terms), REASON_BATCH_SIZE):
            batch = all_removed_terms[i : i + REASON_BATCH_SIZE]
            batch_num = i // REASON_BATCH_SIZE + 1
            print(f"  [{batch_num}/{reason_batches}]", end=" ", flush=True)
            reasons = get_reasons(client, batch)
            all_removed.extend(reasons)
            print(f"{len(reasons)}개 처리")

            lines = [f"{r.get('term', '')} → {r.get('reason', '')}" for r in all_removed]
            stage2_path.write_text("\n".join(lines), encoding="utf-8")
            _save_checkpoint(checkpoint_path, unique_terms, selected, all_removed_terms, all_removed, stage1_done=True, stage2_done=False)

        stage2_done = True
        _save_checkpoint(checkpoint_path, unique_terms, selected, all_removed_terms, all_removed, stage1_done=True, stage2_done=True)
        print(f"이유 수집 완료: {len(all_removed)}개")
        print(f"2단계 저장: {stage2_path}")

    print(f"\n최종 결과: 유지 {len(selected)}개 / 제거 {len(all_removed)}개")

    prompt_text = ", ".join(selected)
    output.write_text(prompt_text, encoding="utf-8")
    print(f"저장 완료: {output} ({len(prompt_text)}자)")

    _write_candidates(candidates_path, selected, all_removed)
    print(f"검토용 목록: {candidates_path}")
    print(f"체크포인트: {checkpoint_path} (재실행 시 자동 재개)")


def _save_checkpoint(path: Path, unique_terms: list, kept: list, removed_terms: list,
                     removed_with_reasons: list, stage1_done: bool, stage2_done: bool):
    data = {
        "terms_count": len(unique_terms),
        "terms_hash": hash(tuple(unique_terms)),
        "stage1_done": stage1_done,
        "stage2_done": stage2_done,
        "kept": kept,
        "removed_terms": removed_terms,
        "removed_with_reasons": removed_with_reasons,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_checkpoint(path: Path, unique_terms: list) -> dict:
    empty = {"kept": [], "removed_terms": [], "removed_with_reasons": [], "stage1_done": False, "stage2_done": False}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("terms_hash") != hash(tuple(unique_terms)):
            print("⚠️  용어 목록이 변경되어 체크포인트를 초기화합니다.")
            path.unlink()
            return empty
        print(f"✅ 체크포인트 발견: 1단계={'완료' if data['stage1_done'] else '진행중'}, 2단계={'완료' if data['stage2_done'] else '진행중'}")
        return data
    except Exception as e:
        print(f"⚠️  체크포인트 로드 실패 ({e}), 처음부터 시작합니다.")
        return empty


def _write_candidates(path: Path, kept: list, removed: list):
    lines = []
    lines.append("=== 유지 ===")
    for t in kept:
        lines.append(f"[유지] {t}")
    lines.append("")
    lines.append("=== 제거 ===")
    for r in removed:
        if isinstance(r, dict):
            term = r.get("term", "")
            reason = r.get("reason", "")
        else:
            term, reason = str(r), ""
        lines.append(f"[제거] {term} → {reason}")
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
