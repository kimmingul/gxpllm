"""
새 study 디렉터리 초기화

분석 의뢰 건마다 표준 디렉터리 구조와 설정을 만든다.
hook 과 runner 가 이 구조를 전제하므로 반드시 이 스크립트로 생성한다.

사용:
    python scripts/init_study.py --root D:\\clinical\\ABC-301 --study-id ABC-301
    python scripts/init_study.py --root D:\\clinical\\ABC-301 --study-id ABC-301 --unblinded
"""

import _common  # noqa: F401  (sys.path 설정)

import argparse
import json
import shutil
import sys
from pathlib import Path

from gxpllm.core import append_audit, now_iso

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

DEFAULT_SAS_EXE = r'C:\Program Files\SASHome\SASFoundation\9.4\sas.exe'
DEFAULT_LLM_ENDPOINT = 'http://dgx-spark.internal:8001/v1'
DEFAULT_LLM_MODEL = 'Qwen3.6-35B-A3B-NVFP4'

# 생성할 디렉터리 (study 루트 기준)
DIRECTORIES = (
    '.gxpllm/env',
    'data/raw',
    'data/derived',
    'docs',
    'docs/csr_draft',
    'programs/sas',
    'programs/python',
    'programs/r',
    'programs/qc',
    'macros',
    'spec',
    'validation',
    'output/tables',
    'output/figures',
    'output/listings',
    'logs/runs',
    'audit',
)

# 각 디렉터리에 둘 안내 파일
READMES = {
    'data/raw': (
        '# 원본 데이터\n\n'
        '분석 의뢰자로부터 받은 원본 데이터셋을 둡니다.\n'
        '**절대 수정하지 마십시오.** 읽기 전용으로 취급합니다.\n\n'
        'Opus 는 이 디렉터리에 접근할 수 없습니다 (hook 차단).\n'
    ),
    'data/derived': (
        '# 파생 데이터셋\n\n'
        '분석 프로그램이 생성한 파생 데이터셋을 둡니다.\n'
        'Opus 는 이 디렉터리에 접근할 수 없습니다 (hook 차단).\n'
    ),
    'output/listings': (
        '# Listing (피험자 단위)\n\n'
        '**Listing 은 피험자 단위 데이터입니다.**\n'
        'Table / Figure 와 달리 Opus 가 읽을 수 없습니다 (hook 차단).\n'
        '검토가 필요하면 사람이 직접 확인하십시오.\n'
    ),
    'output/tables': (
        '# Table (집계 표)\n\n'
        '집계값이므로 Opus 가 읽을 수 있습니다.\n'
        '단, 소규모 셀(n=1, 2)이 있으면 재식별 위험을 검토해야 합니다.\n'
    ),
    'programs/qc': (
        '# Independent Programming (QC)\n\n'
        'double programming 의 QC 쪽 프로그램을 둡니다.\n'
        '**작성 시 primary 프로그램을 참조하지 마십시오.** 독립성이 핵심입니다.\n'
    ),
    'spec': (
        '# 사양서\n\n'
        'table shell, 변수 매핑, 분석 사양 등을 둡니다.\n'
        'Opus 가 읽고 쓸 수 있습니다.\n'
    ),
    'validation': (
        '# 밸리데이션 문서\n\n'
        'URS, 위험평가, IQ/OQ/PQ, SOP, 일탈/CAPA 기록을 둡니다.\n'
        'docs/development.md 12절(알려진 한계)을 반드시 옮겨 기재하십시오.\n'
    ),
    'docs': (
        '# 문서\n\n'
        '- `protocol.md` : 임상시험계획서\n'
        '- `sap.md` : 통계분석계획서 (table shell 포함)\n'
        '- `dmp.md` : 자료관리계획서\n'
        '- `data_dictionary.md` : Data Dictionary (`/build-dictionary` 산출물)\n'
        '- `csr_draft/` : CSR 문구 초안\n\n'
        '이 디렉터리는 피험자 데이터를 담지 않으므로 Opus 가 읽을 수 있습니다.\n'
    ),
}


# ============================================================================
# 메인 로직
# ============================================================================

def build_config(study_id, sas_exe, llm_endpoint, llm_model, blinded):
    """
    study 설정을 구성한다

    Args:
        study_id: study 식별자
        sas_exe: SAS 실행 파일 경로
        llm_endpoint: 로컬 LLM endpoint
        llm_model: 로컬 LLM 모델명
        blinded: 눈가림 여부

    Returns:
        config 딕셔너리
    """
    return {
        'study_id': study_id,
        'created_at': now_iso(),
        'sas_exe': sas_exe,
        'sas_log_encoding': 'cp949',
        'sas_work_root': r'C:\sastemp',
        'llm_endpoint': llm_endpoint,
        'llm_model': llm_model,
        'blinded': blinded,
        '_comment': (
            'allowed_dirs / allowed_log_files / blocked_extensions 를 지정하면 '
            '기본 정책을 덮어씁니다. 기본값은 gxpllm/core.py 를 참조하십시오. '
            '경계를 넓히는 변경은 QA 검토를 거치십시오.'
        ),
    }


def copy_plugin_assets(study_root, plugin_root):
    """
    SAS 매크로 등 study 안에 있어야 하는 파일을 복사한다

    Args:
        study_root: study 루트 Path
        plugin_root: plugin 루트 Path

    Returns:
        복사한 파일 수
    """
    copied = 0

    macro_src = plugin_root / 'macros' / 'gxpllm_assert.sas'
    if macro_src.is_file():
        shutil.copy2(macro_src, study_root / 'macros' / 'gxpllm_assert.sas')
        copied += 1

    return copied


def main():
    """메인 함수"""
    print("=" * 80)
    print("study 디렉터리 초기화")
    print("=" * 80)

    parser = argparse.ArgumentParser(description='새 study 디렉터리를 초기화합니다')
    parser.add_argument('--root', required=True, help='study 루트 경로')
    parser.add_argument('--study-id', required=True, help='study 식별자 (예: ABC-301)')
    parser.add_argument('--sas-exe', default=DEFAULT_SAS_EXE, help='SAS 실행 파일 경로')
    parser.add_argument('--llm-endpoint', default=DEFAULT_LLM_ENDPOINT)
    parser.add_argument('--llm-model', default=DEFAULT_LLM_MODEL)
    parser.add_argument('--unblinded', action='store_true',
                        help='눈가림 해제 상태로 생성합니다 (기본은 눈가림)')
    parser.add_argument('--force', action='store_true',
                        help='이미 존재하는 디렉터리에도 진행합니다')
    args = parser.parse_args()

    study_root = Path(args.root).resolve()
    plugin_root = Path(__file__).resolve().parent.parent
    config_path = study_root / '.gxpllm' / 'config.json'

    # --- 1/4 확인 ----------------------------------------------------------
    print(f"\n[1/4] 대상 확인...")
    print(f"  경로     : {study_root}")
    print(f"  study_id : {args.study_id}")
    print(f"  눈가림   : {'해제' if args.unblinded else '유지'}")

    if config_path.is_file() and not args.force:
        print(f"\n  오류: 이미 초기화된 study 입니다 ({config_path})")
        print(f"        덮어쓰려면 --force 를 지정하십시오.")
        sys.exit(2)

    # --- 2/4 디렉터리 생성 --------------------------------------------------
    print(f"\n[2/4] 디렉터리 생성...")
    for relative in DIRECTORIES:
        (study_root / relative).mkdir(parents=True, exist_ok=True)
    print(f"  {len(DIRECTORIES):,}개 디렉터리 생성")

    for relative, content in READMES.items():
        readme = study_root / relative / 'README.md'
        if not readme.is_file():
            readme.write_text(content, encoding='utf-8')
    print(f"  {len(READMES):,}개 안내 파일 생성")

    # --- 3/4 설정 및 자산 ---------------------------------------------------
    print(f"\n[3/4] 설정 파일 및 자산 복사...")
    config = build_config(
        args.study_id, args.sas_exe, args.llm_endpoint,
        args.llm_model, not args.unblinded,
    )
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"  .gxpllm/config.json 생성")

    copied = copy_plugin_assets(study_root, plugin_root)
    print(f"  자산 {copied:,}개 복사 (SAS 매크로)")

    if not Path(args.sas_exe).is_file():
        print(f"\n  경고: SAS 실행 파일을 찾을 수 없습니다: {args.sas_exe}")
        print(f"        .gxpllm/config.json 의 sas_exe 를 실제 경로로 수정하십시오.")

    # --- 4/4 감사 로그 ------------------------------------------------------
    print(f"\n[4/4] 감사 로그 초기화...")
    append_audit(study_root, {
        'event': 'study_initialized',
        'study_id': args.study_id,
        'blinded': not args.unblinded,
        'plugin_root': str(plugin_root),
    })
    print(f"  audit/audit.jsonl 생성")

    # --- 완료 ---------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("초기화 완료")
    print("=" * 80)
    print(f"""
  다음 단계

  1. 원본 데이터를 배치합니다
       {study_root}\\data\\raw\\

  2. Protocol 을 배치합니다
       {study_root}\\docs\\protocol.md

  3. Claude Code 를 programs 디렉터리에서 실행합니다
       cd {study_root}\\programs

  4. Data Dictionary 를 만듭니다
       /build-dictionary

  5. SAP 초안을 작성합니다
       /draft-sap

  주의
    - data\\ 와 output\\listings\\ 는 Opus 가 읽을 수 없습니다
    - 눈가림 해제 후 .gxpllm/config.json 의 blinded 를 false 로 바꾸십시오
    - audit\\ 와 logs\\ 를 사내 공유 드라이브에 정기 백업하십시오
""")


if __name__ == "__main__":
    main()
