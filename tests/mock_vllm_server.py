"""
vLLM 모의 서버 (테스트용)

DGX Spark 의 실제 vLLM 없이 MCP 서버의 전체 경로를 검증한다.
  MCP 도구 호출 -> HTTP 요청 -> 응답 파싱 -> 코드 추출

OpenAI 호환 endpoint 중 필요한 것만 구현한다.
  GET  /v1/models
  POST /v1/chat/completions

**이 서버는 테스트 전용이다.** 실제 운영에 쓰지 말 것.
검증하는 것은 gxpllm 쪽 코드 경로이지 LLM 품질이 아니다.

사용:
    python tests/mock_vllm_server.py --port 18001
    python tests/mock_vllm_server.py --port 18001 --scenario error
"""

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ============================================================================
# 설정 영역 - 여기서 수정하세요
# ============================================================================

DEFAULT_PORT = 18001
MODEL_ID = 'Qwen3.6-35B-A3B-NVFP4'

# 시나리오별 응답
#   normal        : 정상 코드 생성 (마크다운 펜스 포함 — strip 검증용)
#   no_meta       : GXPLLM-META 블록 없이 반환 (검출 검증용)
#   error         : 500 오류
#   malformed     : 형식이 깨진 JSON
#   slow          : 지연 응답
#   truncated     : finish_reason=length + 잘린 본문
#                   추론 모델이 max_tokens 를 소진한 실제 상황을 재현한다
#   empty_content : finish_reason=stop 인데 content 가 빈 문자열
#                   추론만 하고 본문을 내지 않은 경우
#   null_content  : content 가 null
#   broken_json   : structure_text 요청에 깨진 JSON 반환
SCENARIOS = ('normal', 'no_meta', 'error', 'malformed', 'slow',
             'truncated', 'empty_content', 'null_content', 'broken_json')

# 잘린 응답 — assertion 호출 직전에서 끊긴다.
# 이 문자열이 프로그램으로 저장되면 검증 장치가 통째로 사라진다.
TRUNCATED_RESPONSE = """/*----------------------------------------------------------------------------
  GXPLLM-META-BEGIN
  program      : t_mock.sas
  purpose      : 잘린 응답
  GXPLLM-META-END
----------------------------------------------------------------------------*/

data saf;
    set indata.adsl;
    where SAFFL = 'Y';
run;

%gxpllm_assert_rowc"""

BROKEN_JSON_RESPONSE = '{"symptoms": [{"term": "두통", "evide'

SAS_RESPONSE = """```sas
/*----------------------------------------------------------------------------
  GXPLLM-META-BEGIN
  program      : t_mock.sas
  purpose      : 모의 응답
  sap_ref      : docs/sap.md#mock
  inputs       : data/derived/adsl.sas7bdat
  outputs      : output/tables/t_mock.rtf
  analysis_set : Safety Set (SAFFL='Y')
  author       : local-llm/mock
  GXPLLM-META-END
----------------------------------------------------------------------------*/

%include "&GXPLLM_PLUGIN_ROOT./macros/gxpllm_assert.sas";

libname indata "&GXPLLM_STUDY_ROOT./data/derived" access=readonly;

%gxpllm_assert_rowcount(indata.adsl, label=ADSL_LOADED, expected_min=1);
%gxpllm_assert_unique(indata.adsl, keys=USUBJID, label=ADSL_UNIQUE);

data saf;
    set indata.adsl;
    where SAFFL = 'Y';
run;

%gxpllm_assert_analysis_set(saf, flag_column=SAFFL, flag_value=Y, label=SAFETY_SET);
```"""

PYTHON_RESPONSE = '''"""
모의 응답

GXPLLM-META-BEGIN
program      : t_mock.py
purpose      : 모의 응답
inputs       : data/derived/adsl.parquet
outputs      : output/tables/t_mock.rtf
GXPLLM-META-END
"""

import os
import sys

sys.path.insert(0, os.environ['GXPLLM_PLUGIN_ROOT'] + '/scripts')
import gxpllm_assert as na


def main():
    """메인 함수"""
    na.assert_rowcount([], label='MOCK', expected_min=0)


if __name__ == "__main__":
    main()
'''

NO_META_RESPONSE = """proc print data=adsl;
run;
"""

STRUCTURE_RESPONSE = json.dumps({
    'symptoms': [
        {
            'term': '두통',
            'severity': 'MILD',
            'negated': False,
            'subject': 'patient',
            'evidence': [{'start': 12, 'end': 20, 'text': '경한 두통을 호소'}],
            'confidence': 0.92,
        }
    ]
}, ensure_ascii=False)


# ============================================================================
# 메인 로직
# ============================================================================

class MockHandler(BaseHTTPRequestHandler):
    """OpenAI 호환 최소 handler"""

    scenario = 'normal'
    request_log = []

    def log_message(self, fmt, *args):
        """기본 접근 로그 억제"""

    def _send(self, status, payload):
        """
        JSON 응답을 보낸다

        Args:
            status: HTTP 상태 코드
            payload: 응답 본문 딕셔너리 또는 문자열
        """
        body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        encoded = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        """모델 목록 조회"""
        if self.path.rstrip('/').endswith('/models'):
            self._send(200, {
                'object': 'list',
                'data': [{'id': MODEL_ID, 'object': 'model', 'owned_by': 'vllm'}],
            })
            return
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        """chat completion"""
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b'{}'

        try:
            request = json.loads(raw.decode('utf-8'))
        except ValueError:
            self._send(400, {'error': 'invalid json'})
            return

        MockHandler.request_log.append(request)

        if MockHandler.scenario == 'error':
            self._send(500, {'error': {'message': 'mock internal error'}})
            return

        if MockHandler.scenario == 'malformed':
            self._send(200, '{"choices": [{"broken"')
            return

        if MockHandler.scenario == 'slow':
            import time
            time.sleep(3)

        # 요청 내용으로 어떤 도구인지 추정한다
        content = json.dumps(request, ensure_ascii=False)
        is_structure = '정형화' in content or 'JSON Schema' in content

        if is_structure:
            text = STRUCTURE_RESPONSE
        elif MockHandler.scenario == 'no_meta':
            text = NO_META_RESPONSE
        elif 'Python' in content and 'SAS 9.4' not in content:
            text = PYTHON_RESPONSE
        else:
            text = SAS_RESPONSE

        # --- 잘림 / 빈 본문 재현 --------------------------------------------
        finish_reason = 'stop'
        max_tokens = request.get('max_tokens', 8192)
        completion_tokens = 200

        if MockHandler.scenario == 'truncated':
            # 추론이 예산을 소진해 본문이 중간에서 끊긴 상태
            text = BROKEN_JSON_RESPONSE if is_structure else TRUNCATED_RESPONSE
            finish_reason = 'length'
            completion_tokens = max_tokens
        elif MockHandler.scenario == 'empty_content':
            text = ''
            completion_tokens = max_tokens
        elif MockHandler.scenario == 'null_content':
            text = None
        elif MockHandler.scenario == 'broken_json' and is_structure:
            text = BROKEN_JSON_RESPONSE

        self._send(200, {
            'id': 'mock-1',
            'object': 'chat.completion',
            'model': request.get('model', MODEL_ID),
            'choices': [{
                'index': 0,
                'message': {'role': 'assistant', 'content': text},
                'finish_reason': finish_reason,
            }],
            'usage': {
                'prompt_tokens': 100,
                'completion_tokens': completion_tokens,
                'total_tokens': 100 + completion_tokens,
            },
        })


def start_server(port=DEFAULT_PORT, scenario='normal'):
    """
    모의 서버를 백그라운드 스레드로 시작한다

    Args:
        port: 수신 포트
        scenario: 응답 시나리오

    Returns:
        (HTTPServer 인스턴스, 스레드)
    """
    MockHandler.scenario = scenario
    MockHandler.request_log = []

    server = HTTPServer(('127.0.0.1', port), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main():
    """메인 함수 (독립 실행)"""
    parser = argparse.ArgumentParser(description='vLLM 모의 서버 (테스트 전용)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--scenario', default='normal', choices=SCENARIOS)
    args = parser.parse_args()

    print("=" * 80)
    print("vLLM 모의 서버 (테스트 전용)")
    print("=" * 80)
    print(f"\n  endpoint : http://127.0.0.1:{args.port}/v1")
    print(f"  모델     : {MODEL_ID}")
    print(f"  시나리오 : {args.scenario}")
    print(f"\n  Ctrl+C 로 종료합니다.")

    server, _ = start_server(args.port, args.scenario)
    try:
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
        print("\n  종료했습니다.")


if __name__ == "__main__":
    main()
