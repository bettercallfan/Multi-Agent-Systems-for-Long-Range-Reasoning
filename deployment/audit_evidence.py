"""Read-only recheck of the seven selected historical run directories."""
import json
from pathlib import Path
import sys
from utils.run_audit import audit_run
from utils.injection_audit import audit_runtime_injections


def main():
    root=Path(__file__).resolve().parent
    entries=json.loads((root/'重新审计汇总.json').read_text(encoding='utf-8'))
    passed=True
    for item in entries:
        run=(root/item['group']/item['run_id']).resolve()
        if not run.is_relative_to(root):raise ValueError('Invalid evidence path')
        audit=audit_run(run);passed=passed and audit['passed']
        print(item['run_id'],'run audit',sum(audit['checks'].values()),'/',len(audit['checks']))
        if item['group']=='动态注入恢复证据':
            audit=audit_runtime_injections(run);passed=passed and audit['passed']
            print('  injection audit',sum(audit['checks'].values()),'/',len(audit['checks']))
    print('FINAL:', 'PASS' if passed else 'FAIL')
    return 0 if passed else 1


if __name__=='__main__':sys.exit(main())
