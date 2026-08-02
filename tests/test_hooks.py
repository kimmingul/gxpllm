"""
Hook 동작 검증 테스트

임상 데이터 경계가 실제로 작동하는지 검증한다.
- 정상 차단: 데이터 경로, 직접 실행, 난독화
- 정상 허용: 문서, 코드, runner 경유
- 우회 시도: 상대경로, 대소문자, 인코딩, 셸 트릭

정책상 중요한 검증
- 모든 hook 스크립트가 구문 오류 없이 컴파일되는가
  (구문 오류 시 exit 1 -> Claude Code 가 차단으로 처리하지 않아 경계가 열린다)
- 예외 상황에서 fail-closed 인가

실행:
    python tests/test_hooks.py
"""

import json
import py_compile
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = PLUGIN_ROOT / 'hooks'
RUNNERS_DIR = PLUGIN_ROOT / 'scripts'

GUARD_FILE = HOOKS_DIR / 'guard_file_access.py'
GUARD_BASH = HOOKS_DIR / 'guard_bash.py'

EXIT_ALLOW = 0
EXIT_BLOCK = 2

STUDY_ID = 'TEST-001'

CONFIG_CONTENT = {
    "study_id": STUDY_ID,
    "sas_exe": "C:\\Program Files\\SASHome\\SASFoundation\\9.4\\sas.exe",
    "sas_log_encoding": "cp949",
    "llm_endpoint": "http://dgx-spark.internal:8001/v1",
    "blinded": True,
}


# ============================================================================
# 테스트 케이스 정의
# ============================================================================

def file_access_cases(root):
    """
    파일 접근 hook 테스트 케이스를 만든다

    Args:
        root: 임시 study 루트 경로 문자열

    Returns:
        (설명, tool_name, tool_input, 기대 exit code) 튜플 리스트
    """
    return [
        # --- 차단되어야 하는 것 ---
        ("data 디렉터리 직접 읽기", "Read",
         {"file_path": f"{root}\\data\\raw\\adsl.sas7bdat"}, EXIT_BLOCK),

        ("data 디렉터리 하위 CSV", "Read",
         {"file_path": f"{root}\\data\\derived\\ae.csv"}, EXIT_BLOCK),

        ("listings 디렉터리", "Read",
         {"file_path": f"{root}\\output\\listings\\l_16_2_1.rtf"}, EXIT_BLOCK),

        ("상대경로 우회 시도", "Read",
         {"file_path": "..\\data\\raw\\adsl.sas7bdat"}, EXIT_BLOCK),

        ("슬래시 방향 바꾸기", "Read",
         {"file_path": f"{root}/data/raw/adsl.sas7bdat"}, EXIT_BLOCK),

        ("대문자 우회 시도", "Read",
         {"file_path": f"{root}\\DATA\\RAW\\ADSL.SAS7BDAT"}, EXIT_BLOCK),

        ("확장자만으로 차단 (경로 무관)", "Read",
         {"file_path": "C:\\temp\\copied_adsl.sas7bdat"}, EXIT_BLOCK),

        ("Glob 으로 데이터 탐색", "Glob",
         {"pattern": "data/**/*.sas7bdat"}, EXIT_BLOCK),

        ("Grep 으로 데이터 검색", "Grep",
         {"pattern": "USUBJID", "path": f"{root}\\data"}, EXIT_BLOCK),

        ("Write 로 data 에 쓰기", "Write",
         {"file_path": f"{root}\\data\\derived\\hack.csv"}, EXIT_BLOCK),

        ("눈가림 상태 치료군 파일", "Read",
         {"file_path": f"{root}\\output\\tables\\unblind_summary.rtf"}, EXIT_BLOCK),

        ("경로 중간에 data 삽입", "Read",
         {"file_path": f"{root}\\programs\\..\\data\\raw\\x.sas7bdat"}, EXIT_BLOCK),

        # --- grok C1: runner 출력 경유 유출 ---
        ("runner stdout 읽기", "Read",
         {"file_path": f"{root}\\logs\\runs\\20260802T1-abc\\stdout.txt"}, EXIT_BLOCK),

        ("runner stderr 읽기", "Read",
         {"file_path": f"{root}\\logs\\runs\\20260802T1-abc\\stderr.txt"}, EXIT_BLOCK),

        ("SAS execution.log 읽기", "Read",
         {"file_path": f"{root}\\logs\\runs\\20260802T1-abc\\execution.log"}, EXIT_BLOCK),

        ("SAS execution.lst 읽기", "Read",
         {"file_path": f"{root}\\logs\\runs\\20260802T1-abc\\execution.lst"}, EXIT_BLOCK),

        ("데이터 프로파일 읽기", "Read",
         {"file_path": f"{root}\\logs\\runs\\20260802T1-abc\\profile.json"}, EXIT_BLOCK),

        ("docs 밖 임의 경로로 유출", "Read",
         {"file_path": f"{root}\\_leak.dat"}, EXIT_BLOCK),

        ("scratch 디렉터리 유출", "Read",
         {"file_path": f"{root}\\scratch\\dump.txt"}, EXIT_BLOCK),

        # --- grok C2: Grep/Glob 범위 ---
        ("Grep study 루트 전체", "Grep",
         {"pattern": "USUBJID", "path": root}, EXIT_BLOCK),

        ("Grep 범위 미지정", "Grep",
         {"pattern": "USUBJID"}, EXIT_BLOCK),

        ("Glob 범위 미지정 재귀", "Glob",
         {"pattern": "**/adsl.*"}, EXIT_BLOCK),

        ("Glob study 루트 전체", "Glob",
         {"pattern": "**/*", "path": root}, EXIT_BLOCK),

        ("Grep glob 필터로 csv 탐색", "Grep",
         {"pattern": "USUBJID", "glob": "*.csv"}, EXIT_BLOCK),

        # --- grok C4: 키 별칭 / 미지의 도구 ---
        ("camelCase 키 별칭", "Read",
         {"filePath": f"{root}\\data\\raw\\note.txt"}, EXIT_BLOCK),

        ("target 키 별칭", "SomeNewTool",
         {"target": f"{root}\\data\\raw\\adsl.sas7bdat"}, EXIT_BLOCK),

        ("중첩 구조 안의 경로", "SomeNewTool",
         {"args": {"files": [f"{root}\\data\\derived\\ae.csv"]}}, EXIT_BLOCK),

        ("미지의 도구 + 경로", "MultiEdit",
         {"edits": [{"file": f"{root}\\data\\raw\\x.sas7bdat"}]}, EXIT_BLOCK),

        # --- 4차 검토 CRITICAL-1: 도구명 부분 문자열 우회 ---
        ("catalog_write (cat 포함)", "catalog_write",
         {"file_path": f"{root}\\.gxpllm\\config.json", "content": "pwned"}, EXIT_BLOCK),
        ("concatenate (cat 포함)", "concatenate",
         {"file_path": f"{root}\\audit\\audit.jsonl", "content": "PWN"}, EXIT_BLOCK),
        ("list_write (list 포함)", "list_write",
         {"file_path": f"{root}\\output\\tables\\t_fake.csv", "content": "phi"}, EXIT_BLOCK),
        ("ViewEdit (view 포함)", "ViewEdit",
         {"file_path": f"{root}\\.gxpllm\\config.json", "content": "PWN"}, EXIT_BLOCK),
        ("ReadWrite (read 포함)", "ReadWrite",
         {"file_path": f"{root}\\audit\\audit.jsonl", "content": "PWN"}, EXIT_BLOCK),
        ("FileListEdit", "FileListEdit",
         {"file_path": f"{root}\\.gxpllm\\config.json", "content": "pwned"}, EXIT_BLOCK),
        ("search_replace + 허용 scope", "search_replace",
         {"path": f"{root}\\docs", "file_path": f"{root}\\.gxpllm\\config.json",
          "content": "pwned"}, EXIT_BLOCK),
        ("StrReplace", "StrReplace",
         {"file_path": f"{root}\\.gxpllm\\config.json", "content": "x"}, EXIT_BLOCK),
        ("GlobWrite", "GlobWrite",
         {"file_path": f"{root}\\audit\\audit.jsonl", "content": "x"}, EXIT_BLOCK),

        # --- 정상 읽기 도구는 계속 허용 ---
        ("Read 도구 정상 읽기", "Read",
         {"file_path": f"{root}\\.gxpllm\\config.json"}, EXIT_ALLOW),
        ("NotebookRead", "NotebookRead",
         {"notebook_path": f"{root}\\programs\\python\\a.ipynb"}, EXIT_ALLOW),

        # --- 허용되어야 하는 것 ---
        ("Table CSV 읽기 (정책상 허용)", "Read",
         {"file_path": f"{root}\\output\\tables\\t_14_1_1.csv"}, EXIT_ALLOW),

        ("Table XLSX 읽기 (정책상 허용)", "Read",
         {"file_path": f"{root}\\output\\tables\\t_14_1_1.xlsx"}, EXIT_ALLOW),

        ("manifest.json 읽기", "Read",
         {"file_path": f"{root}\\logs\\runs\\20260802T1-abc\\manifest.json"}, EXIT_ALLOW),

        ("assertions.json 읽기", "Read",
         {"file_path": f"{root}\\logs\\runs\\20260802T1-abc\\assertions.json"}, EXIT_ALLOW),

        ("env_snapshot.json 읽기", "Read",
         {"file_path": f"{root}\\logs\\env_snapshot.json"}, EXIT_ALLOW),

        ("감사 로그 읽기", "Read",
         {"file_path": f"{root}\\audit\\audit.jsonl"}, EXIT_ALLOW),

        ("randomization 문서 (docs 는 눈가림 예외)", "Read",
         {"file_path": f"{root}\\docs\\randomization_plan.md"}, EXIT_ALLOW),

        ("vendor/data 는 programs 하위라 허용", "Read",
         {"file_path": f"{root}\\programs\\vendor\\data\\config.json"}, EXIT_ALLOW),

        ("Grep docs 범위 지정", "Grep",
         {"pattern": "Safety Set", "path": f"{root}\\docs"}, EXIT_ALLOW),

        ("Glob programs 범위 지정", "Glob",
         {"pattern": "**/*.sas", "path": f"{root}\\programs"}, EXIT_ALLOW),

        ("study 밖 파일 읽기", "Read",
         {"file_path": "D:\\repo\\gxpllm-llm\\README.md"}, EXIT_ALLOW),

        # --- 2차 검토: C-NEW-1 config 자가 확장 ---
        ("config.json 쓰기 (정책 자가 확장)", "Write",
         {"file_path": f"{root}\\.gxpllm\\config.json"}, EXIT_BLOCK),

        ("config.json 편집", "Edit",
         {"file_path": f"{root}\\.gxpllm\\config.json"}, EXIT_BLOCK),

        ("config.json 읽기는 허용", "Read",
         {"file_path": f"{root}\\.gxpllm\\config.json"}, EXIT_ALLOW),

        # --- 2차 검토: H-NEW-1 logs allowlist 쓰기 ---
        ("manifest.json 쓰기 (감사 위조)", "Write",
         {"file_path": f"{root}\\logs\\runs\\r1\\manifest.json"}, EXIT_BLOCK),

        ("assertions.json 쓰기", "Write",
         {"file_path": f"{root}\\logs\\runs\\r1\\assertions.json"}, EXIT_BLOCK),

        ("env_snapshot.json 쓰기", "Write",
         {"file_path": f"{root}\\logs\\env_snapshot.json"}, EXIT_BLOCK),

        # --- 2차 검토: H-NEW-5 audit 쓰기 ---
        ("audit.jsonl 쓰기 (차단 기록 말소)", "Write",
         {"file_path": f"{root}\\audit\\audit.jsonl"}, EXIT_BLOCK),

        ("audit.jsonl 읽기는 허용", "Read",
         {"file_path": f"{root}\\audit\\audit.jsonl"}, EXIT_ALLOW),

        # --- 산출물 직접 생성 차단 (runner 만 생성) ---
        ("output/tables 직접 쓰기", "Write",
         {"file_path": f"{root}\\output\\tables\\fake.csv"}, EXIT_BLOCK),

        # --- 쓰기 허용 영역 ---
        ("programs 쓰기", "Write",
         {"file_path": f"{root}\\programs\\python\\t_ae.py"}, EXIT_ALLOW),

        ("docs 쓰기", "Write",
         {"file_path": f"{root}\\docs\\sap.md"}, EXIT_ALLOW),

        ("spec 디렉터리 읽기", "Read",
         {"file_path": f"{root}\\spec\\table_shells.md"}, EXIT_ALLOW),

        ("validation 디렉터리 읽기", "Read",
         {"file_path": f"{root}\\validation\\iq_oq_pq.md"}, EXIT_ALLOW),

        ("SAP 문서 읽기", "Read",
         {"file_path": f"{root}\\docs\\sap.md"}, EXIT_ALLOW),

        ("Data Dictionary 읽기", "Read",
         {"file_path": f"{root}\\docs\\data_dictionary.md"}, EXIT_ALLOW),

        ("SAS 프로그램 읽기", "Read",
         {"file_path": f"{root}\\programs\\sas\\t_dm_summary.sas"}, EXIT_ALLOW),

        ("Table 결과 읽기", "Read",
         {"file_path": f"{root}\\output\\tables\\t_14_1_1.rtf"}, EXIT_ALLOW),

        ("Figure 읽기", "Read",
         {"file_path": f"{root}\\output\\figures\\f_14_2_1.png"}, EXIT_ALLOW),

        ("프로그램 작성", "Write",
         {"file_path": f"{root}\\programs\\python\\t_ae.py"}, EXIT_ALLOW),

        ("manifest 읽기", "Read",
         {"file_path": f"{root}\\logs\\runs\\20260802T1-abc\\manifest.json"}, EXIT_ALLOW),
    ]


def bash_cases(root):
    """
    셸 명령 hook 테스트 케이스를 만든다

    Args:
        root: 임시 study 루트 경로 문자열

    Returns:
        (설명, 명령, 기대 exit code) 튜플 리스트
    """
    return [
        # --- 차단되어야 하는 것 ---
        ("sas.exe 직접 실행", "sas.exe -sysin programs/sas/t_dm.sas", EXIT_BLOCK),
        ("전체 경로 SAS 실행",
         '"C:\\Program Files\\SASHome\\SASFoundation\\9.4\\sas.exe" -sysin x.sas', EXIT_BLOCK),
        ("Rscript 직접 실행", "Rscript --vanilla programs/r/f_km.R", EXIT_BLOCK),
        ("R CMD BATCH", "R CMD BATCH programs/r/f_km.R", EXIT_BLOCK),
        ("python 스크립트 직접 실행", "python programs/python/t_ae.py", EXIT_BLOCK),
        ("uv run 우회", "uv run python programs/python/t_ae.py", EXIT_BLOCK),
        ("데이터 파일 cat", f"type {root}\\data\\raw\\adsl.csv", EXIT_BLOCK),
        ("Get-Content 로 데이터 읽기", f"Get-Content {root}\\data\\derived\\ae.csv", EXIT_BLOCK),
        ("데이터 복사", f"copy {root}\\data\\raw\\*.sas7bdat C:\\temp\\", EXIT_BLOCK),
        ("PowerShell 인코딩 명령", "powershell -enc SQBFAFgAIAA=", EXIT_BLOCK),
        ("certutil 디코딩", "certutil -decode payload.txt out.exe", EXIT_BLOCK),
        ("Invoke-Expression", "IEX (Get-Content script.ps1)", EXIT_BLOCK),
        ("saspy 사용", "python -c \"import saspy; saspy.SASsession()\"", EXIT_BLOCK),
        ("파이프로 데이터 전송", f"type {root}\\data\\raw\\adsl.csv | findstr USUBJID", EXIT_BLOCK),
        ("xpt 파일 참조", "cp study.xpt /tmp/", EXIT_BLOCK),

        # --- grok C3: python -c 임의 코드 실행 ---
        ("python -c 인라인 실행", 'python -c "print(1)"', EXIT_BLOCK),
        ("python -c 경로 문자열 결합",
         'python -c "p=r\'' + root + '\';print(open(p+chr(92)+\'da\'+\'ta\').read())"', EXIT_BLOCK),
        ("python -c Path 결합",
         'python -c "from pathlib import Path;print((Path(r\'' + root + '\')/\'data\').read_text())"',
         EXIT_BLOCK),
        ("python -m 모듈 실행", "python -m http.server", EXIT_BLOCK),
        ("node -e 인라인 실행", 'node -e "console.log(1)"', EXIT_BLOCK),
        ("py 런처", "py -3 script.py", EXIT_BLOCK),

        # --- grok C3 연장: 명령 체이닝 우회 ---
        ("runner 뒤 python -c 체이닝",
         'python scripts/run_sas.py --program programs/sas/t.sas && python -c "print(1)"', EXIT_BLOCK),
        ("runner 뒤 데이터 읽기 체이닝",
         f'python scripts/run_sas.py --program programs/sas/t.sas; type {root}\\data\\raw\\x.csv',
         EXIT_BLOCK),
        ("runner 파이프 우회",
         f'python scripts/run_python.py --program p.py | Get-Content {root}\\data\\raw\\x.csv',
         EXIT_BLOCK),

        # --- grok H1: 경로 리터럴 없는 재귀 탐색 ---
        ("Get-ChildItem -Recurse",
         f"Get-ChildItem -Path {root} -Recurse -Filter adsl.*", EXIT_BLOCK),
        ("findstr /s 재귀 검색",
         f"findstr /s /i USUBJID {root}\\*.*", EXIT_BLOCK),
        ("dir /s 재귀 목록", f"dir /s {root}", EXIT_BLOCK),
        ("Select-String 검색", f"Select-String -Pattern USUBJID -Path {root}", EXIT_BLOCK),
        ("ripgrep 재귀", f"rg USUBJID {root}", EXIT_BLOCK),
        ("Join-Path 경로 조립",
         "powershell -Command \"$p=Join-Path 'X' ('da'+'ta'); Get-Content $p\"", EXIT_BLOCK),
        ("for 반복 실행", f"for /d %i in ({root}\\d*) do @type %i\\raw\\note.txt", EXIT_BLOCK),
        ("ForEach-Object 반복",
         f"Get-ChildItem {root} | ForEach-Object {{ Get-Content $_.FullName }}", EXIT_BLOCK),
        ("아카이브로 대량 반출",
         f"Compress-Archive -Path {root} -DestinationPath C:\\temp\\out.zip", EXIT_BLOCK),
        ("robocopy 대량 복사", f"robocopy {root}\\data C:\\temp /E", EXIT_BLOCK),

        # --- grok C1: 실행 출력 읽기 ---
        ("stdout.txt cat", f"type {root}\\logs\\runs\\r1\\stdout.txt", EXIT_BLOCK),
        ("execution.log cat", f"type {root}\\logs\\runs\\r1\\execution.log", EXIT_BLOCK),

        # --- 명령 치환 ---
        ("백틱 명령 치환", "echo `cat secret`", EXIT_BLOCK),
        ("달러 명령 치환", "echo $(cat secret)", EXIT_BLOCK),

        # --- 허용되어야 하는 것 ---
        ("SAS runner 경유", "python scripts/run_sas.py --program programs/sas/t_dm.sas", EXIT_ALLOW),
        ("Python runner 경유", "python scripts/run_python.py --program programs/python/t_ae.py", EXIT_ALLOW),
        ("R runner 경유", "python scripts/run_r.py --program programs/r/f_km.R", EXIT_ALLOW),
        ("감사 검증", "python scripts/verify_audit.py --study .", EXIT_ALLOW),
        ("git status", "git status", EXIT_ALLOW),
        ("git diff", "git diff --stat", EXIT_ALLOW),
        ("출력 디렉터리 목록", f"dir {root}\\output\\tables", EXIT_ALLOW),
        ("문서 확인", f"type {root}\\docs\\sap.md", EXIT_ALLOW),
        ("runner purpose 지정",
         "python scripts/run_sas.py --program programs/sas/t.sas --purpose submission_candidate",
         EXIT_ALLOW),

        # --- 2차 검토: C-NEW-2 스크립트 간접 실행 ---
        ("powershell -File 스크립트", "powershell -File programs/exfil.ps1", EXIT_BLOCK),
        ("cmd /c 배치 파일", "cmd.exe /c programs\\exfil.bat", EXIT_BLOCK),
        ("pwsh 래퍼", "pwsh -Command Get-Content x", EXIT_BLOCK),
        ("sh -c 래퍼", 'sh -c "echo hi"', EXIT_BLOCK),
        ("wsl 래퍼", "wsl cat /mnt/d/clinical/x", EXIT_BLOCK),

        # --- 2차 검토: C-NEW-3 따옴표 래핑 우회 ---
        ("cmd.exe /c 따옴표 안 python -c",
         'cmd.exe /c "python -c print(1)"', EXIT_BLOCK),
        ("sh -c 따옴표 안 python -c",
         'sh -c "python -c print(1)"', EXIT_BLOCK),
        ("따옴표 안 Rscript",
         'cmd /c "Rscript script.R"', EXIT_BLOCK),

        # --- 2차 검토: H-NEW-2 git grep -- data ---
        ("git grep -- data", "git grep USUBJID -- data", EXIT_BLOCK),
        ("git grep 전체", "git grep USUBJID", EXIT_BLOCK),
        ("git show 객체 조회", "git show HEAD:data/raw/adsl.csv", EXIT_BLOCK),

        # --- 2차 검토: tree 디렉터리 구조 노출 ---
        ("tree /F", f"tree {root} /F", EXIT_BLOCK),

        # --- 3차 검토 CRITICAL-1: prefix + 확장자 없는 스크립트 ---
        ("env + 확장자 없는 python 스크립트",
         "env python programs/python/payload", EXIT_BLOCK),
        ("call + 확장자 없는 python",
         "call python programs/python/payload", EXIT_BLOCK),
        ("timeout + 확장자 없는 python",
         "timeout 5 python programs/python/payload", EXIT_BLOCK),
        ("env pythonw 확장자 없음",
         "env pythonw programs/python/payload", EXIT_BLOCK),
        ("nice + Rscript", "nice Rscript programs/r/payload", EXIT_BLOCK),
        ("중첩 prefix", "env timeout 5 python payload", EXIT_BLOCK),
        ("xargs 경유", "xargs python payload", EXIT_BLOCK),
        ("env 변수 지정 후 python", "env VAR=1 python payload", EXIT_BLOCK),

        # --- 3차 검토 CRITICAL-2: 변수 확장 간접 실행 ---
        ("Windows 환경변수 확장", 'set PY=python && %PY% -c "print(1)"', EXIT_BLOCK),
        ("%VAR% 단독 사용", '%PY% programs/x', EXIT_BLOCK),
        ("셸 변수 확장", 'PY=python; $PY -c "print(1)"', EXIT_BLOCK),
        ("PowerShell 환경변수", '$env:PY = "python"', EXIT_BLOCK),

        # --- 6차 검토 HIGH-1: 쓰기 가능 디렉터리에 심은 인터프리터 ---
        ("programs 하위 임의 경로 python",
         "programs/leak/python payload", EXIT_BLOCK),
        ("programs 하위 python3", "programs/leak/python3 payload", EXIT_BLOCK),
        ("programs 하위 py", "programs/leak/py payload", EXIT_BLOCK),
        ("programs 하위 pythonw", "programs/x/pythonw payload", EXIT_BLOCK),
        ("programs 하위 node", "programs/leak/node script.js", EXIT_BLOCK),
        ("programs 하위 perl", "programs/a/perl payload", EXIT_BLOCK),
        ("macros 에 심은 python", "macros/python payload", EXIT_BLOCK),
        ("env + programs 하위 python",
         "env programs/leak/python payload", EXIT_BLOCK),

        # --- 6차 검토 HIGH-2: git 범위 표기 ---
        ("git diff 두 점 범위", "git diff HEAD~1..HEAD", EXIT_BLOCK),
        ("git diff 세 점 범위", "git diff master...HEAD", EXIT_BLOCK),
        ("git diff caret bang", "git diff HEAD^!", EXIT_BLOCK),
        ("git -c 후 범위 diff", "git -c core.pager=cat diff HEAD~1..HEAD", EXIT_BLOCK),
        ("git log 범위", "git log HEAD~3..HEAD", EXIT_BLOCK),

        # --- 6차 검토 MEDIUM-3: 변수 부분 확장 ---
        ("변수 부분 확장", "call %PY:~0% -c print(1)", EXIT_BLOCK),
        ("변수 범위 확장", "call %PY:~0,10% payload", EXIT_BLOCK),
        ("변수 치환 확장", "call %PY:python=python% payload", EXIT_BLOCK),

        # --- 6차 검토 LOW-7: 미등록 셸 ---
        ("mksh 래퍼", "mksh -c id", EXIT_BLOCK),
        ("yash 래퍼", "yash -c id", EXIT_BLOCK),
        ("xonsh 래퍼", "xonsh -c id", EXIT_BLOCK),

        # --- 5차 검토 CRITICAL-1: command 는 실제로 실행한다 ---
        ("command python -c", "command python -c print(1)", EXIT_BLOCK),
        ("command -- python", "command -- python -c print(1)", EXIT_BLOCK),
        ("command.com /c python", "command.com /c python -c print(1)", EXIT_BLOCK),
        ("command python payload", "command python payload", EXIT_BLOCK),

        # --- 5차 검토 CRITICAL-2: Windows 경로 인터프리터 (.exe 없음) ---
        ("C:\\Python310\\python", "C:\\Python310\\python payload", EXIT_BLOCK),
        ("D:\\miniconda3\\python", "D:\\miniconda3\\python payload", EXIT_BLOCK),
        ("상대경로 ..\\python", "..\\python payload", EXIT_BLOCK),
        ("깊은 상대경로", "..\\..\\Python310\\python payload", EXIT_BLOCK),
        ("pythonw 경로", "C:\\Python310\\pythonw payload", EXIT_BLOCK),
        ("py 런처 경로", "C:\\Windows\\py -3 -c print(1)", EXIT_BLOCK),
        ("ruby 경로", "C:\\Ruby\\bin\\ruby script.rb", EXIT_BLOCK),
        ("perl 경로", "C:\\Perl\\bin\\perl script.pl", EXIT_BLOCK),

        # --- 5차 검토 CRITICAL-3: git alias 우회 ---
        ("git alias 로 show", "git -c alias.s=show s HEAD", EXIT_BLOCK),
        ("git alias 로 외부 실행", "git -c alias.x=!python x payload", EXIT_BLOCK),
        ("git alias 조합", "git -c core.pager=cat -c alias.s=show s HEAD", EXIT_BLOCK),

        # --- 5차 검토 HIGH-4: git 이 첫 토큰이 아닌 경우 ---
        ("env git show", "env git show HEAD", EXIT_BLOCK),
        ("env git log -p", "env git log -p", EXIT_BLOCK),
        ("timeout git log -p", "timeout 5 git log -p", EXIT_BLOCK),
        ("nice git show", "nice git show HEAD", EXIT_BLOCK),
        ("env.exe git show", "env.exe git show HEAD", EXIT_BLOCK),
        ("git status 뒤 env git show",
         "git status && env git show HEAD", EXIT_BLOCK),

        # --- 5차 검토 HIGH-5: call 접두 변수 확장 ---
        ("call %PY% -c", "call %PY% -c print(1)", EXIT_BLOCK),
        ("call %PY% payload", "call %PY% payload", EXIT_BLOCK),
        ("call !PY! -c", "call !PY! -c print(1)", EXIT_BLOCK),

        # --- 5차 검토 HIGH-6: git 내용 출력 하위명령 ---
        ("git whatchanged -p", "git whatchanged -p", EXIT_BLOCK),
        ("git log --full-diff", "git log --all --full-diff", EXIT_BLOCK),
        ("git diff 커밋 두 개", "git diff HEAD~1 HEAD", EXIT_BLOCK),

        # --- 5차 검토 MEDIUM-7: busybox ash ---
        ("busybox ash", "busybox ash -c id", EXIT_BLOCK),
        ("tcsh 래퍼", "tcsh -c id", EXIT_BLOCK),

        # --- 4차 검토 CRITICAL-2: 래퍼 목록 우회 ---
        ("깊은 래퍼 중첩", "env env env env env env python payload", EXIT_BLOCK),
        ("래퍼 .exe 접미사", "env.exe python payload", EXIT_BLOCK),
        ("timeout.exe", "timeout.exe 5 python payload", EXIT_BLOCK),
        ("timeout 단위 인자", "timeout 5s python payload", EXIT_BLOCK),
        ("timeout Windows /T", "timeout /T 5 python payload", EXIT_BLOCK),
        ("env -u 옵션 인자", "env -u HOME python payload", EXIT_BLOCK),
        ("runas /flag", "runas /user:admin python payload", EXIT_BLOCK),
        ("busybox 래퍼", "busybox python payload", EXIT_BLOCK),
        ("taskset 래퍼", "taskset 0x1 python payload", EXIT_BLOCK),
        ("numactl 래퍼", "numactl --cpunodebind=0 python payload", EXIT_BLOCK),
        ("firejail 래퍼", "firejail python payload", EXIT_BLOCK),
        ("systemd-run 래퍼", "systemd-run --user python payload", EXIT_BLOCK),
        ("nsenter 래퍼", "nsenter -t 1 -m python payload", EXIT_BLOCK),
        ("flock 래퍼", "flock /tmp/l python payload", EXIT_BLOCK),
        ("pipenv run", "pipenv run python payload", EXIT_BLOCK),
        ("pdm run", "pdm run python payload", EXIT_BLOCK),
        ("hatch run", "hatch run python payload", EXIT_BLOCK),
        ("parallel 래퍼", "parallel python ::: payload", EXIT_BLOCK),
        ("coverage run", "coverage run programs/python/payload", EXIT_BLOCK),

        # --- 4차 검토 CRITICAL-3: 간접 실행 ---
        ("for 반복으로 python", "for %A in (python) do %A payload", EXIT_BLOCK),
        ("for 반복 + -c", "for %A in (python) do %A -c print(1)", EXIT_BLOCK),
        ("forfiles", "forfiles /m *.py /c python @file", EXIT_BLOCK),
        ("wmic 프로세스 생성", "wmic process call create python", EXIT_BLOCK),
        ("mshta", "mshta vbscript:Execute(msgbox)", EXIT_BLOCK),
        ("setx 환경변수", "setx PY python", EXIT_BLOCK),
        ("rundll32", "rundll32 shell32.dll,Control_RunDLL", EXIT_BLOCK),
        ("schtasks", "schtasks /create /tn x /tr python", EXIT_BLOCK),

        # --- 4차 검토 HIGH-4: git 전역 옵션 우회 ---
        ("git --no-pager log -p", "git --no-pager log -p", EXIT_BLOCK),
        ("git -c 옵션 후 log -p", "git -c core.pager=cat log -p", EXIT_BLOCK),
        ("git -C 후 log -p", "git -C . log -p", EXIT_BLOCK),
        ("git -C 후 show 데이터", "git -C . show HEAD:data/raw/x.csv", EXIT_BLOCK),
        ("git format-patch", "git format-patch -1 HEAD", EXIT_BLOCK),
        ("git format-patch --stdout", "git format-patch HEAD~1 --stdout", EXIT_BLOCK),
        ("git log -U3", "git log -U3", EXIT_BLOCK),
        ("git log --unified=3", "git log --unified=3", EXIT_BLOCK),

        # --- 4차 검토 HIGH-5: 지연 확장 ---
        ("지연 확장 명령 위치", "!PY! -c print(1)", EXIT_BLOCK),

        # --- 3차 검토 HIGH-3: git log -p ---
        ("git log -p", "git log -p", EXIT_BLOCK),
        ("git log --patch --all", "git log --patch --all", EXIT_BLOCK),
        ("git stash show -p", "git stash show -p", EXIT_BLOCK),

        # --- 3차 검토: 첫 토큰이 인터프리터가 아닌 우회 ---
        ("env 로 감싼 python -c", 'env python -c "print(1)"', EXIT_BLOCK),
        ("nice 로 감싼 python", "nice python programs/python/t_ae.py", EXIT_BLOCK),
        ("timeout 으로 감싼 python -c", 'timeout 10 python -c "x=1"', EXIT_BLOCK),
        ("따옴표 친 첫 토큰 python", '"python" -c "print(1)"', EXIT_BLOCK),
        ("전체 경로 cmd.exe 래퍼",
         'C:\\Windows\\System32\\cmd.exe /c python -c "print(1)"', EXIT_BLOCK),
        ("start 로 배치 실행", "start evil.bat", EXIT_BLOCK),
        ("cscript VBS 실행", "cscript evil.vbs", EXIT_BLOCK),
        ("wscript VBS 실행", "wscript evil.vbs", EXIT_BLOCK),
        ("bash -c 데이터 읽기", 'bash -c "cat data/raw/x"', EXIT_BLOCK),
        ("git cat-file", "git cat-file -p abc123", EXIT_BLOCK),
        ("git archive", "git archive HEAD", EXIT_BLOCK),
        ("bare data 인자", "ls data", EXIT_BLOCK),

        # --- 2차 검토: 오탐 해소 확인 (허용되어야 함) ---
        ("pytest 실행", "pytest programs/python", EXIT_ALLOW),
        ("git diff 프로그램", "git diff programs/python/t_ae.py", EXIT_ALLOW),
        ("where sas", "where sas", EXIT_ALLOW),
        ("findstr 재귀 아님", f"findstr /n USUBJID {root}\\docs\\protocol.md", EXIT_ALLOW),
        ("Get-ChildItem docs", f"Get-ChildItem {root}\\docs", EXIT_ALLOW),
        ("echo", "echo hello", EXIT_ALLOW),

        # --- 통계 프로그래머 일상 명령 (모두 허용되어야 한다) ---
        #
        # 오탐은 사용자가 plugin 을 끄게 만드는 가장 현실적인 우회 경로다.
        # 따라서 오탐 회귀를 보안 회귀와 동일하게 다룬다.
        ("git add 프로그램", "git add programs/sas/t_dm.sas", EXIT_ALLOW),
        ("git branch", "git branch -a", EXIT_ALLOW),
        ("git checkout -b", "git checkout -b feature/ae-summary", EXIT_ALLOW),
        ("git remote", "git remote -v", EXIT_ALLOW),
        ("git config 조회", "git config user.name", EXIT_ALLOW),
        ("git stash list", "git stash list", EXIT_ALLOW),
        ("파일 복사", "copy programs\\sas\\t1.sas programs\\sas\\t2.sas", EXIT_ALLOW),
        ("파일 삭제", "del programs\\sas\\old.sas", EXIT_ALLOW),
        ("디렉터리 생성", "mkdir output\\tables\\draft", EXIT_ALLOW),
        ("파일 비교", "fc programs\\sas\\a.sas programs\\sas\\b.sas", EXIT_ALLOW),
        ("black --check", "black --check programs/python", EXIT_ALLOW),
        ("에디터 실행", "code programs/sas/t_dm.sas", EXIT_ALLOW),
        ("메모장", "notepad docs\\sap.md", EXIT_ALLOW),
        ("화면 지우기", "cls", EXIT_ALLOW),
        ("benchmark runner",
         "python scripts/benchmark_codegen.py --cases benchmark/cases.yaml --study .",
         EXIT_ALLOW),

        # --- 3차 검토: bare 토큰 오탐 해소 (허용되어야 함) ---
        ("echo 문장에 data 포함", "echo update the data dictionary", EXIT_ALLOW),
        ("echo data 단독", "echo data", EXIT_ALLOW),
        ("echo logs", "echo logs", EXIT_ALLOW),
        ("git commit 메시지에 data", 'git commit -m "update data dictionary"', EXIT_ALLOW),
        ("data_dictionary 문서 읽기", f"type {root}\\docs\\data_dictionary.md", EXIT_ALLOW),
        ("git log 기본", "git log --oneline -5", EXIT_ALLOW),

        # --- 4차 검토: 변수 확장 오탐 해소 (허용되어야 함) ---
        ("echo 안의 $VAR", "echo check $nobs in log", EXIT_ALLOW),
        ("echo 안의 ${VAR}", "echo use ${nobs} from SAS", EXIT_ALLOW),
        ("echo 안의 %VAR%", "echo Today is %DATE%", EXIT_ALLOW),
        ("echo 날짜 시각", "echo %DATE% %TIME%", EXIT_ALLOW),
        ("set 환경변수 설정", "set PURPOSE=exploratory", EXIT_ALLOW),
        ("PowerShell 변수 출력", "echo $ErrorActionPreference", EXIT_ALLOW),
        ("git log format", "git log --format=%H", EXIT_ALLOW),
        ("퍼센트 문자 포함", "echo 50% complete", EXIT_ALLOW),

        # --- 4차 검토: 조회 명령 오탐 해소 ---
        ("command -v python", "command -v python", EXIT_ALLOW),
        ("which python", "which python", EXIT_ALLOW),
        ("Get-Command python", "Get-Command python", EXIT_ALLOW),

        # --- 5차 검토: echo 경로 언급 오탐 해소 (허용되어야 함) ---
        ("echo logs 경로 언급", "echo see logs/runs for results", EXIT_ALLOW),
        ("echo listings 언급", "echo see output/listings note", EXIT_ALLOW),
        ("echo profile.json 언급", "echo profile.json is metadata", EXIT_ALLOW),
        ("echo stdout.txt 언급", "echo stdout.txt appears in logs", EXIT_ALLOW),
        ("echo 백슬래시 경로", "echo path is logs\\runs\\abc", EXIT_ALLOW),

        # --- 5차 검토: git 정상 사용은 계속 허용 ---
        ("git diff 파일 하나", "git diff programs/sas/t.sas", EXIT_ALLOW),
        ("git blame 프로그램", "git blame programs/sas/t.sas", EXIT_ALLOW),
        ("git show 없이 status", "git status --short", EXIT_ALLOW),

        # --- 6차 검토: 오탐 해소 (허용되어야 함) ---
        ("./ 접두 프로그램 디렉터리", "ruff check ./programs/python", EXIT_ALLOW),
        ("역슬래시 ./ 접두", "mypy .\\programs\\python", EXIT_ALLOW),
        ("programs/r 디렉터리", "ruff check programs/r", EXIT_ALLOW),
        ("커밋 메시지에 alias.", 'git commit -m "update alias.docs"', EXIT_ALLOW),
        ("grep 옵션에 alias.", "git log --grep=alias.foo", EXIT_ALLOW),
        ("설정값에 alias 도메인",
         "git -c user.email=foo@alias.com status", EXIT_ALLOW),
        ("git diff 경로 지정", "git diff HEAD -- programs/x.sas", EXIT_ALLOW),
        ("git diff 파일만", "git diff programs/x.sas", EXIT_ALLOW),

        # --- 3차 검토: bare 토큰은 파일 읽기 명령에서만 차단 ---
        ("findstr 로 data 디렉터리 검색", "findstr USUBJID data", EXIT_BLOCK),
        ("type data", "type data", EXIT_BLOCK),
        ("ruff check programs/python", "ruff check programs/python", EXIT_ALLOW),
        ("mypy programs/python", "mypy programs/python", EXIT_ALLOW),
        ("dir programs\\python", f"dir {root}\\programs\\python", EXIT_ALLOW),
        ("Get-ChildItem programs\\r", f"Get-ChildItem {root}\\programs\\r", EXIT_ALLOW),
        ("black 포맷", "black programs/python/t_ae.py", EXIT_ALLOW),
        ("Select-String docs 범위",
         f"Select-String -Path {root}\\docs\\sap.md -Pattern Safety", EXIT_ALLOW),
        ("init_study runner", "python scripts/init_study.py --root X --study-id Y", EXIT_ALLOW),
        ("compare_outputs runner",
         "python scripts/compare_outputs.py --primary a.json --qc b.json", EXIT_ALLOW),
        ("where python", "where python", EXIT_ALLOW),
        ("git log", "git log --oneline -10", EXIT_ALLOW),
    ]


# ============================================================================
# 실행 유틸
# ============================================================================

def invoke_hook(script, payload):
    """
    hook 스크립트를 실행하고 결과를 돌려준다

    Args:
        script: hook 스크립트 경로
        payload: stdin 으로 보낼 딕셔너리

    Returns:
        (exit_code, stderr 문자열)
    """
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    result = subprocess.run(
        [sys.executable, str(script)],
        input=data,
        capture_output=True,
        timeout=30,
    )
    stderr = (result.stderr or b'').decode('utf-8', errors='replace')
    return result.returncode, stderr


def make_temp_study(tmpdir):
    """
    테스트용 임시 study 디렉터리를 만든다

    Args:
        tmpdir: 임시 디렉터리 경로

    Returns:
        study 루트 Path
    """
    root = Path(tmpdir) / STUDY_ID
    for sub in ('.gxpllm', 'data/raw', 'data/derived', 'docs',
                'programs/sas', 'programs/python', 'programs/r',
                'output/tables', 'output/figures', 'output/listings',
                'logs/runs', 'audit', 'spec', 'validation', 'macros'):
        (root / sub).mkdir(parents=True, exist_ok=True)

    with open(root / '.gxpllm' / 'config.json', 'w', encoding='utf-8') as f:
        json.dump(CONFIG_CONTENT, f, ensure_ascii=False, indent=2)

    return root


# ============================================================================
# 메인 로직
# ============================================================================

def test_compile_all():
    """
    모든 hook / runner 스크립트가 구문 오류 없이 컴파일되는지 검증한다

    구문 오류가 있으면 exit code 1 이 되고, Claude Code 는 이를 차단으로
    처리하지 않으므로 경계가 열린다. 배포 전 반드시 통과해야 한다.

    Returns:
        (통과 수, 실패 수, 실패 목록)
    """
    print("\n[1/5] 스크립트 컴파일 검증...")
    targets = sorted(HOOKS_DIR.glob('*.py'))
    if RUNNERS_DIR.is_dir():
        targets += sorted(RUNNERS_DIR.glob('*.py'))
    targets += sorted((PLUGIN_ROOT / 'gxpllm').glob('*.py'))

    passed, failures = 0, []
    for path in targets:
        try:
            py_compile.compile(str(path), doraise=True, cfile=None)
            passed += 1
            print(f"  OK   {path.relative_to(PLUGIN_ROOT)}")
        except Exception as exc:
            failures.append(f"{path.relative_to(PLUGIN_ROOT)}: {exc}")
            print(f"  FAIL {path.relative_to(PLUGIN_ROOT)}  <-- {exc}")

    return passed, len(failures), failures


def test_file_access(root):
    """
    파일 접근 hook 을 검증한다

    Args:
        root: 임시 study 루트 Path

    Returns:
        (통과 수, 실패 수, 실패 목록)
    """
    print(f"\n[3/5] 파일 접근 hook 검증...")
    passed, failures = 0, []

    for desc, tool_name, tool_input, expected in file_access_cases(str(root)):
        payload = {
            'session_id': 'test',
            'cwd': str(root / 'programs'),
            'hook_event_name': 'PreToolUse',
            'tool_name': tool_name,
            'tool_input': tool_input,
        }
        code, stderr = invoke_hook(GUARD_FILE, payload)
        label = '차단' if expected == EXIT_BLOCK else '허용'

        if code == expected:
            passed += 1
            print(f"  OK   [{label}] {desc}")
        else:
            failures.append(f"[{label} 기대] {desc}: exit {code}")
            print(f"  FAIL [{label}] {desc}  <-- exit {code}")
            if stderr.strip():
                print(f"       {stderr.strip().splitlines()[0]}")

    return passed, len(failures), failures


def test_bash(root):
    """
    셸 명령 hook 을 검증한다

    Args:
        root: 임시 study 루트 Path

    Returns:
        (통과 수, 실패 수, 실패 목록)
    """
    print(f"\n[4/5] 셸 명령 hook 검증...")
    passed, failures = 0, []

    for desc, command, expected in bash_cases(str(root)):
        payload = {
            'session_id': 'test',
            'cwd': str(root / 'programs'),
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_input': {'command': command},
        }
        code, stderr = invoke_hook(GUARD_BASH, payload)
        label = '차단' if expected == EXIT_BLOCK else '허용'

        if code == expected:
            passed += 1
            print(f"  OK   [{label}] {desc}")
        else:
            failures.append(f"[{label} 기대] {desc}: exit {code}")
            print(f"  FAIL [{label}] {desc}  <-- exit {code}")
            if stderr.strip():
                print(f"       {stderr.strip().splitlines()[0]}")

    return passed, len(failures), failures


def test_fail_closed(root):
    """
    비정상 입력에서 fail-closed 인지 검증한다

    Args:
        root: 임시 study 루트 Path

    Returns:
        (통과 수, 실패 수, 실패 목록)
    """
    print(f"\n[5/5] fail-closed 검증...")
    passed, failures = 0, []

    cases = [
        ("빈 입력", b''),
        ("잘못된 JSON", b'{not json'),
        ("JSON 배열", b'[]'),
        ("BOM 포함 정상 JSON", '\ufeff{"tool_name":"Read","tool_input":{"file_path":"'
                              + str(root).replace('\\', '\\\\')
                              + '\\\\data\\\\raw\\\\x.sas7bdat"},"cwd":"'
                              + str(root).replace('\\', '\\\\') + '"}'),
    ]

    for desc, raw in cases:
        data = raw.encode('utf-8') if isinstance(raw, str) else raw
        result = subprocess.run(
            [sys.executable, str(GUARD_FILE)],
            input=data, capture_output=True, timeout=30,
        )
        if result.returncode == EXIT_BLOCK:
            passed += 1
            print(f"  OK   [차단] {desc}")
        else:
            failures.append(f"{desc}: exit {result.returncode} (차단 기대)")
            print(f"  FAIL [차단] {desc}  <-- exit {result.returncode}")

    return passed, len(failures), failures


def test_hook_wiring():
    """
    hooks.json 의 matcher 가 guard_file_access 의 위임 대상을 모두 받는지 검증한다

    guard_file_access 는 셸 도구를 guard_bash 로 넘긴다. 넘기기만 하고
    hooks.json 이 그 도구를 guard_bash 에 배선하지 않으면, 그 도구는
    두 hook 을 모두 통과한다. 실제로 PowerShell 이 이 상태였다.

    Returns:
        (통과 수, 실패 수, 실패 목록)
    """
    print("\n[2/5] hook 배선 검증...")
    passed, failures = 0, []

    with open(HOOKS_DIR / 'hooks.json', encoding='utf-8') as f:
        config = json.load(f)

    matchers = [
        entry.get('matcher', '')
        for event in ('PreToolUse', 'PostToolUse')
        for entry in config.get('hooks', {}).get(event, [])
        if any('guard_bash' in h.get('command', '') or 'audit_append' in h.get('command', '')
               for h in entry.get('hooks', []))
    ]

    # guard_file_access 가 넘기는 이름을 소스에서 직접 읽는다 (import 없이)
    source = (HOOKS_DIR / 'guard_file_access.py').read_text(encoding='utf-8')
    match = re.search(r'SHELL_TOOL_NAMES\s*=\s*frozenset\(\{([^}]*)\}\)', source)
    delegated = sorted(re.findall(r"'([^']+)'", match.group(1))) if match else []

    if not delegated:
        failures.append("guard_file_access.SHELL_TOOL_NAMES 를 읽을 수 없습니다")
        print("  FAIL SHELL_TOOL_NAMES 파싱 실패")
        return passed, len(failures), failures

    # 'shell' 은 가상의 별칭이라 실제 도구 이름이 아니다. 나머지는 배선되어야 한다.
    for name in delegated:
        if name == 'shell':
            continue
        covered = [m for m in matchers
                   if name in [part.lower() for part in m.split('|')]]
        # PreToolUse(guard_bash) 와 PostToolUse(audit_append) 양쪽에 있어야 한다
        if len(covered) >= 2:
            passed += 1
            print(f"  OK   [배선] {name} -> guard_bash + audit_append")
        else:
            failures.append(
                f"{name}: guard_file_access 가 위임하는데 hooks.json matcher 에 없습니다 "
                f"(matchers={matchers})"
            )
            print(f"  FAIL [배선] {name}  <-- matcher 누락")

    return passed, len(failures), failures


def main():
    """메인 함수"""
    print("=" * 80)
    print("gxpllm Hook 검증")
    print("=" * 80)

    total_passed = 0
    total_failed = 0
    all_failures = []

    for test_fn in (test_compile_all, test_hook_wiring):
        p, f, fails = test_fn()
        total_passed += p
        total_failed += f
        all_failures += fails

    with tempfile.TemporaryDirectory() as tmpdir:
        root = make_temp_study(tmpdir)
        print(f"\n  임시 study: {root}")

        for test_fn in (test_file_access, test_bash, test_fail_closed):
            p, f, fails = test_fn(root)
            total_passed += p
            total_failed += f
            all_failures += fails

    print(f"\n{'=' * 80}")
    print(f"결과: {total_passed:,}건 통과 / {total_failed:,}건 실패")
    if all_failures:
        print("\n실패 목록:")
        for item in all_failures:
            print(f"  - {item}")
    print("=" * 80)

    sys.exit(1 if total_failed else 0)


if __name__ == "__main__":
    main()
