"""Owner-requested Markdown reports with untrusted content rendered as literal text."""
import re


STATUSES = {'backlog': 'В очереди', 'planning': 'Планирование', 'ready': 'План готов',
            'running': 'Выполняется', 'paused': 'Приостановлена', 'done': 'Завершена',
            'failed': 'Ошибка', 'cancelled': 'Отменена', 'review': 'На согласовании',
            'pending': 'Ожидает'}


def literal(value):
    value = str(value or '')
    longest = max((len(x) for x in re.findall(r'`+', value)), default=0)
    fence = '`' * max(3, longest + 1)
    return f'{fence}text\n{value}\n{fence}'


def task_report(task, project=None, agents=()):
    names = {a['id']: a.get('name', a['id']) for a in agents}
    lines = ['# Отчёт по задаче', '', literal(task.get('title')), '',
             'Статус: ' + STATUSES.get(task.get('status'), 'Неизвестен'), '',
             '## Задание', '', literal(task.get('brief')), '']
    if project:
        lines += ['## Текущий контекст проекта', '', literal(project.get('name')), '',
                  literal(project.get('description')), '']
    lines += ['## Итог', '', literal(task.get('result') or 'Итог пока не сформирован.'), '']
    if task.get('error'):
        lines += ['## Ошибка задачи', '', literal(task['error']), '']
    lines += ['## Этапы', '']
    for index, step in enumerate(task.get('steps', []), 1):
        details = '\n'.join([
            'Название: ' + str(step.get('name', '')),
            'Агент: ' + names.get(step.get('agent_id'), step.get('agent_id', 'Не назначен')),
            'Статус: ' + STATUSES.get(step.get('status'), 'Неизвестен'),
            'Зависимости: ' + (', '.join(step.get('depends_on', [])) or 'Нет'),
            'Инструкция: ' + str(step.get('input', '')),
        ])
        lines += [f'### Этап {index}', '', literal(details), '',
                  literal(step.get('output') or 'Результат пока не получен.'), '']
        if step.get('error'):
            lines += ['Ошибка:', '', literal(step['error']), '']
    lines += ['## Учёт выполнения', '',
              literal(f"ID: {task['id']}\nОбновлено: {task.get('updated_at', '')}\n"
                      f"Входные токены: {task.get('input_tokens_used', 0)}\n"
                      f"Выходные токены: {task.get('output_tokens_used', 0)}\n"
                      f"Лимит выходных токенов: {task.get('max_output_tokens', 0)}"), '',
              'Расход содержит неопределённые значения.' if task.get('usage_uncertain') else
              'Данные учёта сохранены приложением; денежную стоимость отчёт не рассчитывает.', '']
    return '\n'.join(lines)
