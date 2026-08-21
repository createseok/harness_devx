"""기동 중인 서버에 워크스페이스를 하나 만들어 넣는다.

    PYTHONPATH=. .venv/bin/python scripts/seed_api.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
WS = "ws_demo"

AGENTS = [
    ("기획자", "너는 결제팀 PM이다. 요청을 받으면 스스로 다 하려 하지 말고 "
              "적임자에게 위임하고, 결과를 종합해 사람에게 보고한다."),
    ("데이터분석가", "너는 데이터 분석가다. 지표를 쪼개서 원인을 특정하고 숫자로 말한다. "
                  "데이터가 없으면 없다고 한다."),
    ("개발자", "너는 백엔드 개발자다. 코드 레벨 원인과 구체적인 조치안을 "
             "우선순위와 함께 제시한다."),
]


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main() -> int:
    human = post("/api/humans", {"workspace_id": WS, "name": "석"})
    channel = post("/api/channels", {"workspace_id": WS, "name": "결제-이슈",
                                     "topic": "결제 실패율 급증 대응"})
    post(f"/api/channels/{channel['id']}/members",
         {"member_type": "human", "member_id": human["id"]})

    for name, role in AGENTS:
        agent = post("/api/agents", {"workspace_id": WS, "name": name,
                                     "role_prompt": role})
        post(f"/api/channels/{channel['id']}/members",
             {"member_type": "agent", "member_id": agent["id"]})
        print(f"  에이전트 @{name}  {agent['id']}")

    print(f"\n채널  {channel['id']}  (#{channel['name']})")
    print(f"사람  {human['id']}  ({human['name']})")
    print(f"""
메시지 보내기:
  curl -X POST {BASE}/api/channels/{channel['id']}/messages \\
    -H 'Content-Type: application/json' \\
    -d '{{"author_id":"{human['id']}","author_name":"{human['name']}","text":"@기획자 도와주세요"}}'

실시간 구독:
  curl -N {BASE}/api/channels/{channel['id']}/stream
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
