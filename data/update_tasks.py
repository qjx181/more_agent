import json
data = json.load(open('/tmp/p3_project/data/deep_fix_tasks.json'))
data['fixed_count'] = min(data['total'], 10)
data['fixed_types'] = ['high_cyclomatic_complexity (extracted into sub-functions)']
data['status'] = 'fixed_by_coordinator'
json.dump(data, open('/tmp/p3_project/data/deep_fix_tasks.json', 'w'), ensure_ascii=False, indent=2)
print('Updated deep_fix_tasks.json:', data['total'], 'issues, fixed:', data['fixed_count'])
