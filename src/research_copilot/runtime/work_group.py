"""Work-group identity shared by collection and execution anti-bypass limits."""

import hashlib
import json


def work_group_id(scope, review_id):
    return hashlib.sha256(
        json.dumps([scope.model_dump(), review_id], sort_keys=True).encode()
    ).hexdigest()
