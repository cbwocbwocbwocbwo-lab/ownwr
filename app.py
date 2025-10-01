import io
import re
import zipfile
import pdfplumber
import pandas as pd
import streamlit as st

st.set_page_config(page_title="주요 등기사항 요약 추출기", layout="wide")

# ===== 규칙/키워드 =====
# '기타'에서 제외할 권리들 (별도 O/X 컬럼이 있는 것들은 모두 제외)
EXCLUDED_RIGHTS = {"근저당", "가등기", "가압류", "압류"}
OTHER_RIGHTS_CANDIDATES = {
    "경매", "임의경매", "강제경매", "경매개시", "경매개시결정",
    "지상권", "법정지상권", "전세권",
    "가처분", "예고등기", "유치권", "환매특약", "환매권",
    "분묘기지권", "대지권"
}
SUMMARY_ANCHOR = "주요 등기사항 요약"

# ===== 유틸 =====
def normalize(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()

def find_gabgu_rows(lines):
    """
    '2. 소유권에 관한 사항 (갑구)' 구간에서
    표의 '행'(예: '2 소유권이전 ...')처럼 보이는 줄만 리스트로 추출.
    - 제목/머릿글(순위번호/등기목적/접수정보/주요등기사항/대상소유자) 무시
    - '3.'(다음 섹션) 또는 '[ 참고사항 ]' 앞에서 종료
    """
    rows = []
    in_block = False
    for t in map(normalize, lines):
        if not in_block and t.startswith("2.") and ("갑구" in t or "소유권에 관한 사항" in t):
            in_block = True
            continue
        if in_block:
            # 다음 섹션 또는 참고사항에서 종료 (두 표기 모두 허용)
            if t.startswith("3.") or (t.startswith("[") and ("참 고 사 항" in t or "참고사항" in t)):
                break
            if any(h in t for h in ("순위번호", "등기목적", "접수정보", "주요등기사항", "대상소유자")):
                continue
            if re.match(r"^\s*\d+\s+", t):
                rows.append(t)
    return rows

def extract_owners(lines, start_idx, window=80):
    """
    '고유번호' 인근에서 소유주 이름만 추출.
    1) 라벨형 '소유자:' / 2) '(소유자)/(공유자)' 인접줄(짧을 때 윗줄과 결합) /
    3) 갑구 표(세로표형/헤더형) 순서로 시도.
    """
    lo = max(0, start_idx - 15)
    hi = min(len(lines), start_idx + window)
    seg = [(ln or "").strip() for ln in lines[lo:hi]]

    names = []

    # (1) 라벨형 "소유자:" / "소유주:"
    p_label = re.compile(r"(?:소유자|소유주)\s*[:：]\s*([^\n]+)")
    for line in seg:
        m = p_label.search(line)
        if m:
            raw = re.sub(r"\([^)]*\)", "", m.group(1))
            raw = re.sub(r"(지분|각|공동)\s*", "", raw)
            parts = re.split(r"[,\s/·]+", raw)
            for p in parts:
                p = p.strip()
                if re.fullmatch(r"[가-힣A-Za-z0-9\(\)·]{2,40}", p):
                    names.append(p)

    # (2) '(소유자)/(공유자)' 줄
    for i, line in enumerate(seg):
        if "(소유자)" in line or "(공유자)" in line:
            base = line.split("(")[0].strip()
            # 같은 줄의 앞부분이 너무 짧으면(예: '원', '행') 바로 윗줄과 결합해 복원
            def prev_non_empty(k):
                j = k - 1
                while j >= 0 and not seg[j].strip():
                    j -= 1
                return seg[j] if j >= 0 else ""

            base_no_space = base.replace(" ", "")
            if len(base_no_space) <= 2:
                prev = prev_non_empty(i).replace(" ", "")
                base = (prev + base_no_space)
            else:
                base = base_no_space

            # 등록번호·보조 문구·역할 괄호 제거
            base = re.split(r"\d{6,}-\d{3,}", base)[0]
            base = re.split(r"(단독소유|공유|각|지분|주소|소재지|주\s*소)", base)[0]
            base = re.sub(r"\((?:소유자|공유자|수탁자|위탁자)\)", "", base)
            base = normalize(base)
            if re.search(r"[가-힣A-Za-z]", base):
                names.append(base)

    # (3) 갑구 표 기반(세로표형/헤더형)
    if not names:
        cand = extract_owner_from_gabgu_table(seg)
        if cand:
            names.append(cand)

    # 순서 보존 중복 제거
    seen, ordered = set(), []
    for n in names:
        n = n.strip()
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)

    return ", ".join(ordered) if ordered else "미확인"

def extract_owner_from_gabgu_table(lines_window):
    """
    '1. 소유자현황(갑구)' 표에서 등기명의인 추출(세로표형/헤더형 모두 지원).
    - 라벨 '등기명의인'은 공백이 섞인 '등 기 명 의 인'도 인식
    - 라벨 아래 1~5줄을 공백 없이 이어붙여 단어 복원
    - 괄호 속 역할표시는 제거
    """
    def nospace(s: str) -> str:
        return normalize(s).replace(" ", "")

    # ===== A) 헤더형: '등기명의인 (주민)등록번호 ...' 패턴
    header_idx = None
    for i, t in enumerate(lines_window):
        s = normalize(t)
        if ("등기명의인" in s or "등 기 명 의 인" in s) and ("등록번호" in s or "주민" in s):
            header_idx = i
            break
    if header_idx is not None:
        body = []
        for j in range(header_idx + 1, min(header_idx + 5, len(lines_window))):
            x = normalize(lines_window[j])
            if x:
                body.append(x)
        row = " ".join(body)
        if row:
            m = re.search(r"^(.+?)\s+\d{6,}-\d{3,}", row)  # 등록번호 "앞"까지
            cand = m.group(1).strip() if m else None
            if cand:
                cand = re.split(r"(단독소유|공유|각|지분|주소|소재지|주\s*소|순위번호)", cand)[0]
                cand = re.sub(r"\((?:소유자|공유자|수탁자|위탁자|등기명의인)\)", "", cand)
                cand = re.sub(r"\s+", " ", cand).strip()
                if re.search(r"[가-힣A-Za-z]", cand):
                    return cand

    # ===== B) 세로표형: '등기명의인' 라벨(공백 무시) 아래 1~5줄을 '공백 없이' 이어붙임
    idx = None
    for i, t in enumerate(lines_window):
        if nospace(t) == "등기명의인":
            idx = i
            break
    if idx is not None:
        parts = []
        for j in range(idx + 1, min(idx + 6, len(lines_window))):
            s = normalize(lines_window[j])
            if s:
                parts.append(s)
        if parts:
            row = "".join(p.replace(" ", "") for p in parts)  # 단어 복원(공백 제거)
            # 등록번호/머리글 앞에서 자르기
            row = re.split(r"\d{6,}-\d{3,}", row)[0]
            row = re.split(r"(단독소유|공유|각|지분|주소|소재지|주\s*소|순위번호)", row)[0]
            # 괄호 속 역할표시 제거
            row = re.sub(r"\((?:소유자|공유자|수탁자|위탁자|등기명의인)\)", "", row)
            row = normalize(row)
            if re.search(r"[가-힣A-Za-z]", row):
                return row

    return None

def extract_uid_and_address(lines, idx):
    line = lines[idx]
    m = re.search(r"고유번호\s*[:：]?\s*([0-9\-–]+)", line)
    uid = m.group(1).replace("–", "-") if m else "미확인"
    addr = "미확인"
    j = idx + 1
    while j < len(lines):
        nxt = normalize(lines[j])
        if nxt:
            addr = re.sub(r"^(주소|소재지)\s*[:：]?\s*", "", nxt).strip()
            break
        j += 1
    return uid, addr

# ===== 요약(을구) 블록 파싱 보조 =====
def find_eulgu_rows(lines):
    """
    '3. (근)저당권 및 전세권 등 (을구)' 구간에서
    표의 '행'처럼 보이는 줄만 리스트로 추출.
    """
    rows = []
    in_block = False
    for ln in lines:
        t = normalize(ln)
        if not in_block and t.startswith("3.") and ("을구" in t or "전세권" in t or "저당권" in t):
            in_block = True
            continue
        if in_block:
            # 종료 조건
            if (t.startswith("[") and ("참 고 사 항" in t or "참고사항" in t)) or t.startswith("4."):
                break
            # 머릿글 무시
            if any(h in t for h in ("순위번호", "등기목적", "접수정보", "주요등기사항", "대상소유자")):
                continue
            # 행 패턴: 맨 앞에 숫자
            if re.match(r"^\s*\d+\s+", t):
                rows.append(t)
    return rows

  

def eulgu_is_empty(lines):
    """
    을구 블록이 '기록사항 없음'이면 True.
    (제목 바로 아래 1~3줄 범위에서 확인)
    """
    for i, ln in enumerate(lines):
        t = normalize(ln)
        if t.startswith("3.") and ("을구" in t or "전세권" in t or "저당권" in t):
            for j in range(1, 4):
                if i + j < len(lines) and "기록사항 없음" in normalize(lines[i + j]):
                    return True
    return False

def scan_rights_flags_from_rows(rows):
    """
    을구 '행'만 가지고 권리 플래그 계산 → O/X
    - 근저당: '근저당' 또는 '저당권'
    - 가등기: '가등기'
    - 압류:   '압류' (단, '가압류'는 제외)
    - 가압류: '가압류'
    """
    text = "\n".join(rows)
    geun = "O" if ("근저당" in text or "저당권" in text) else "X"
    ga_deung = "O" if "가등기" in text else "X"
    ga_ap = "O" if "가압류" in text else "X"
    ap = "O" if re.search(r"(?<!가)압류", text) else "X"
    return geun, ga_deung, ap, ga_ap, text

def collect_other_rights_from_rows(rows):
    """을구 '행'들만 보고 3/4종(근저당/가등기/가압류/압류) 제외 권리명 수집"""
    text = "\n".join(rows)
    found = {kw for kw in OTHER_RIGHTS_CANDIDATES if kw in text}
    found -= EXCLUDED_RIGHTS
    return ", ".join(sorted(found)) if found else "없음"

def collect_other_rights_respecting_gachobeon(eul_rows, gabgu_rows):
    """
    '기타' 권리 수집 시, '가처분'은 갑구에서만 인정하도록 제한.
    - 전체(을구+갑구) 텍스트에서 권리 후보를 찾되,
      '가처분'은 갑구 텍스트에 없으면 제외.
    """
    text_all = "\n".join(eul_rows + gabgu_rows)
    found = {kw for kw in OTHER_RIGHTS_CANDIDATES if kw in text_all}
    found -= EXCLUDED_RIGHTS  # 근저당/가등기/가압류/압류는 기존대로 제외

    gabgu_text = "\n".join(gabgu_rows)
    if "가처분" in found and "가처분" not in gabgu_text:
        found.remove("가처분")

    return ", ".join(sorted(found)) if found else "없음"

def find_summary_page_index(pdf):
    for i, page in enumerate(pdf.pages):
        txt = page.extract_text() or ""
        if SUMMARY_ANCHOR in txt:
            return i
    return None

def parse_single_pdf(file_like, filename):
    rows_out = []
    with pdfplumber.open(file_like) as pdf:
        start = find_summary_page_index(pdf)
        if start is None:
            return rows_out

        pages = pdf.pages[start:start + 1]
        all_text = "\n".join([normalize(p.extract_text() or "") for p in pages])
        lines = [ln for ln in all_text.splitlines() if ln.strip()]

        # ===== 을구/갑구 판정 및 기타/재계약여부 계산 =====
        if eulgu_is_empty(lines):
    # 을구 비었어도 갑구는 확인해야 함
            geun, ga_deung = "X", "X"  # 근저당/가등기는 원칙적으로 을구 항목
    # 갑구
            gabgu_rows = find_gabgu_rows(lines)
            gabgu_text = "\n".join(gabgu_rows)
    # [변경] 가등기: 을구가 비어도 갑구에서 발견하면 O
            ga_deung = "O" if "가등기" in gabgu_text else "X"
    # [변경] 가등기: 갑구에서도 검색하여 을구 결과와 병합(OR)
            ga_deung = "O" if (ga_deung == "O" or "가등기" in gabgu_text) else "X"
    # 기타 = 갑구에서 추출
            other_rights = collect_other_rights_from_rows(gabgu_rows)
    # 압류/가압류 플래그는 갑구에서 계산
            ap, ga_ap = scan_gabgu_flags(lines)
    # 을구 텍스트는 공란
            eul_text = ""
        else:
            # 을구
            eul_rows = find_eulgu_rows(lines)
            geun, ga_deung, ap_eul, ga_ap_eul, eul_text = scan_rights_flags_from_rows(eul_rows)

            # 갑구
            gabgu_rows = find_gabgu_rows(lines)
            gabgu_text = "\n".join(gabgu_rows)
            # [변경] 가등기: 갑구에서도 검색하여 을구 결과와 병합(OR)
            ga_deung = "O" if (ga_deung == "O" or "가등기" in gabgu_text) else "X"


            # 기타 = 갑구+을구
            other_rights = collect_other_rights_respecting_gachobeon(eul_rows, gabgu_rows)

            # 갑구(압류/가압류) 병합(안전망)

# (else 분기 내부)
            ap_gabgu, ga_ap_gabgu = scan_gabgu_flags(lines)
            ap    = "O" if (ap_eul == "O" or re.search(r"(?<!가)압류", gabgu_text) or ap_gabgu == "O") else "X"
            ga_ap = "O" if (ga_ap_eul == "O" or "가압류" in gabgu_text or ga_ap_gabgu == "O") else "X"

        # 재계약여부: 가등기 O 또는 (갑구/을구 텍스트에 경매/경매개시/경매개시결정) 또는 압류 O → X
        both_text = f"{eul_text}\n{gabgu_text}"
        has_gadeung = (ga_deung == "O")
        has_gyeongmae = any(k in both_text for k in ["경매", "경매개시", "경매개시결정"])
        has_apryu = (ap == "O")
        has_gachobeon = ("가처분" in gabgu_text)
        re_contract = "X" if (has_gadeung or has_gyeongmae or has_apryu or has_gachobeon) else "O"

        # ===== 고유번호 단위 레코드 생성 =====
        for i, ln in enumerate(lines):
            if "고유번호" in ln:
                uid, addr = extract_uid_and_address(lines, i)
                owners = extract_owners(lines, i, window=40)
                rows_out.append({
                    "화일명": filename,
                    "소유주": owners,
                    "고유번호": uid,
                    "주소": addr,
                    "근저당": geun,
                    "가등기": ga_deung,
                    "압류": ap,
                    "가압류": ga_ap,
                    "기타": other_rights,
                    "재계약여부": re_contract,
                })
    return rows_out

def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="결과")
    return buffer.getvalue()

def scan_gabgu_flags(lines):
    in_block = False
    rows = []
    for t in map(normalize, lines):
        if not in_block and t.startswith("2.") and ("갑구" in t or "소유권에 관한 사항" in t):
            in_block = True
            continue
        if in_block:
            if t.startswith("3.") or t.startswith("["):
                break  # 다음 섹션 또는 참고사항에서 종료
            if re.match(r"^\s*\d+\s+", t):
                rows.append(t)
    text = "\n".join(rows)
    ap_gabgu = "O" if re.search(r"(?<!가)압류", text) else "X"
    ga_ap_gabgu = "O" if "가압류" in text else "X"
    return ap_gabgu, ga_ap_gabgu

def iterate_input_files(uploaded_files):
    """PDF + ZIP 모두 처리"""
    for uf in uploaded_files or []:
        name = uf.name
        data = uf.read()
        if name.lower().endswith(".pdf"):
            yield io.BytesIO(data), name
        elif name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        if info.filename.lower().endswith(".pdf"):
                            pdf_bytes = zf.read(info)
                            inner_name = f"{name}::{info.filename}"
                            yield io.BytesIO(pdf_bytes), inner_name
            except zipfile.BadZipFile:
                st.error(f"❌ ZIP 손상 또는 형식 오류: {name}")

# ===== Streamlit UI =====
st.title("📄 주요 등기사항 요약 ➜ 표 추출 · 엑셀 변환기")
st.caption(
    "규칙: 소유주 이름만(괄호표기 제거, 다수면 쉼표로), 주소는 고유번호 바로 아래 줄. "
    "근저당/가등기/압류/가압류는 O/X, 기타는 권리명만. "
    "재계약여부는 (가등기·경매·압류 중 하나라도 있으면 X, 아니면 O). "
    "※ 분석 범위: '주요 등기사항 요약' 1쪽, 갑구+을구의 '행' 전체를 참조(압류/가압류 병합 포함)"
)

uploaded_files = st.file_uploader(
    "PDF 또는 ZIP 파일을 선택하세요 (여러 개 가능)",
    type=["pdf", "zip"],
    accept_multiple_files=True
)

run = st.button("실행")

if run:
    all_rows = []
    any_processed = False

    for file_like, fname in iterate_input_files(uploaded_files):
        any_processed = True
        rows = parse_single_pdf(file_like, fname)
        if not rows:
            st.warning(f"⚠️ '{fname}'에서 '{SUMMARY_ANCHOR}' 페이지를 찾지 못해 건너뜀")
        else:
            all_rows.extend(rows)

    if not any_processed:
        st.error("업로드된 파일이 없습니다.")
    elif not all_rows:
        st.error("추출된 데이터가 없습니다. PDF 형식/스캔 여부를 확인해 주세요.")
    else:
        df = pd.DataFrame(all_rows)
        # 순번 부여
        df.insert(0, "순번", range(1, len(df) + 1))
        # 열 순서 고정 (요청 반영)
        desired_cols = ["순번","화일명","소유주","고유번호","주소","근저당","가등기","압류","가압류","기타","재계약여부"]
        df = df[[col for col in desired_cols if col in df.columns]]
        st.success(f"총 {len(df)}건 추출 완료")
        st.dataframe(df, use_container_width=True)

        xlsx = to_excel_bytes(df)
        st.download_button(
            label="💾 엑셀(.xlsx) 다운로드",
            data=xlsx,
            file_name="등기_요약_추출결과.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
