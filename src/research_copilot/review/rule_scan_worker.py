"""Child process for compliance regex scans. Reads JSON on stdin, writes hits on stdout."""

import json
import sys

from research_copilot.review.scan import scan_rules_inprocess


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("扫描输入必须是 JSON 对象")
        hits = scan_rules_inprocess(payload)
    except Exception as exc:  # noqa: BLE001 - 子进程要把失败暴露给父进程
        print(f"SCAN-ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(
        json.dumps(
            {"hits": [item.to_payload() for item in hits]},
            ensure_ascii=False,
        ).encode("utf-8")
    )
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
