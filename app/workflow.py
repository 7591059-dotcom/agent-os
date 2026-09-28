"""Persisted idea review shared by web and Telegram. Approval never publishes."""
import asyncio
import json

from app.db import db, now
from app.engine import EngineError, _BOUNDARY, _integer, _text
from app.providers import complete, ProviderError


class Workflow:
    def __init__(self, engine):
        self.engine = engine

    def create_idea(self, text, *, config, source='web', connector_id=None, user_id=None):
        text = _text(text, 'Идея', 16000)
        if source not in {'web', 'telegram'}:
            raise EngineError('Неизвестный источник идеи.')
        planner = config.get('planner_provider_id')
        intake_provider = config.get('intake_provider_id') or planner
        self.engine._provider(planner)
        self.engine._provider(intake_provider)
        instructions = _text(config.get('intake_instructions', ''), 'Инструкции приёмного агента', 8000, False)
        if source == 'telegram' and (not isinstance(connector_id, str) or not connector_id or
                                     not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0):
            raise EngineError('Для Telegram требуется подключение и ID пользователя.')
        task = self.engine.create_task({'title': text.splitlines()[0][:160] or 'Новая идея', 'brief': text,
            'planner_provider_id': planner, 'max_steps': config.get('max_steps', 8),
            'max_output_tokens': config.get('max_output_tokens', 24000), 'project_id':config.get('project_id','')})
        task.update(status='review', source=source, intake={
            'state': 'queued', 'revision': 1, 'proposal': '', 'original_text': text,
            'provider_id': intake_provider, 'instructions': instructions, 'feedback': [],
            'reserved_tokens': 0, 'error': ''})
        if connector_id and type(user_id) is int and user_id > 0:
            task['telegram'] = {'connector_id': connector_id, 'user_id': user_id}
        saved = db.put('task', task)
        db.event(task['id'], 'Идея принята. Приёмный агент подготовит предложение для согласования.')
        return saved

    @staticmethod
    def _check(task, revision, states):
        _integer(revision, 'Версия предложения', 1, 1000000)
        intake = task.get('intake') or {}
        if task.get('status') != 'review' or intake.get('state') not in states:
            raise EngineError('Это действие недоступно в текущем состоянии идеи.')
        if intake.get('revision') != revision:
            raise EngineError('Предложение уже изменилось. Откройте актуальную версию.')
        return intake

    async def approve(self, task_id, revision):
        def change(task):
            intake = self._check(task, revision, {'review'})
            self.engine._provider(task.get('planner_provider_id'))
            if task['max_output_tokens'] - task.get('output_tokens_used', 0) < 256:
                raise EngineError('Недостаточно токенов для планирования. Увеличьте бюджет задачи.')
            brief = ('Исходная идея:\n' + intake['original_text'] + '\n\nУточнения владельца:\n' +
                     '\n'.join(intake['feedback']) + '\n\nСогласованное предложение:\n' + intake['proposal'])
            brief = _text(brief, 'Итоговое задание; попросите сократить предложение через правки', 30000)
            intake.update(state='approved', approved_at=now())
            task.update(status='backlog', auto_run=True, error='', brief=brief)
            return task
        saved = db.mutate('task', task_id, change)
        if saved is None:
            raise EngineError('Задача не найдена.')
        # Engine.action performs no I/O await before persisting its transition.
        result = await self.engine.action(task_id, 'plan')
        db.event(task_id, 'Предложение одобрено. Оркестратор создаёт план и запускает агентов; публикация требует отдельного согласования.')
        return result

    def revise(self, task_id, feedback, revision):
        feedback = _text(feedback, 'Правки', 4000)
        def change(task):
            intake = self._check(task, revision, {'review', 'failed'})
            if sum(map(len, intake['feedback'])) + len(feedback) > 8000 or len(intake['feedback']) >= 12:
                raise EngineError('Лимит истории правок исчерпан. Создайте новую идею с итоговыми требованиями.')
            self.engine._provider(intake['provider_id'])
            intake['feedback'].append(feedback)
            intake.update(state='queued', revision=revision + 1, error='', proposal='')
            task['error'] = ''
            return task
        saved = db.mutate('task', task_id, change)
        if saved is None:
            raise EngineError('Задача не найдена.')
        db.event(task_id, 'Правки сохранены. Приёмный агент подготовит новую версию.')
        return saved

    def reject(self, task_id, revision):
        def change(task):
            intake = self._check(task, revision, {'queued', 'analyzing', 'review', 'failed'})
            intake['state'] = 'rejected'
            task['status'] = 'cancelled'
            return task
        saved = db.mutate('task', task_id, change)
        if saved is None:
            raise EngineError('Задача не найдена.')
        db.event(task_id, 'Идея отклонена. Исполняющие агенты не запущены.')
        return saved

    def recover(self):
        for task in db.list('task'):
            intake = task.get('intake') or {}
            if intake.get('state') != 'analyzing' and not intake.get('reserved_tokens'):
                continue
            reserved = intake.get('reserved_tokens', 0)
            if reserved:
                self.engine._charge(task, {'input_tokens': 0, 'output_tokens': reserved,
                                          'source': 'interrupted_upper_bound'})
            intake['reserved_tokens'] = 0
            if task['status'] == 'review' and intake.get('state') == 'analyzing':
                intake.update(state='failed', error='Анализ прерван перезапуском. Отправьте правки для явного повтора; возможный расход уже учтён.')
                task['error'] = intake['error']
            db.put('task', task)

    async def loop(self, stop_event):
        self.recover()
        while not stop_event.is_set():
            for task in reversed(db.list('task')):
                if stop_event.is_set():
                    return
                if task.get('status') == 'review' and task.get('intake', {}).get('state') == 'queued':
                    await self._analyze(task['id'])
            try:
                await asyncio.wait_for(stop_event.wait(), .3)
            except TimeoutError:
                pass

    async def _analyze(self, task_id):
        if self.engine._slots is None:
            raise EngineError('Движок ещё не запущен.')
        revision = None
        reserved = 0
        settled = False
        try:
            async with self.engine._slots:
                task = db.get('task', task_id)
                if not task or task['status'] != 'review' or task['intake']['state'] != 'queued':
                    return
                intake = task['intake']
                revision = intake['revision']
                provider = self.engine._provider(intake['provider_id'])
                reserved = min(2000, task['max_output_tokens'] - task.get('output_tokens_used', 0))
                if reserved < 256:
                    reserved = 0
                    raise EngineError('Недостаточно выходных токенов для анализа. Увеличьте бюджет задачи и отправьте правки.')
                intake.update(state='analyzing', reserved_tokens=reserved)
                db.put('task', task)
                system = ('AGENT_OS_INTAKE\n' + _BOUNDARY + '\nВы — приёмный агент. Разберите идею, предложите '
                          'конкретное решение, этапы, роли агентов, ожидаемые результаты, ограничения и необходимые данные. '
                          'Не создавайте агентов и ничего не запускайте. Завершите вопросом: одобрить реализацию или внести правки? '
                          'Отдельно укажите, что публикация требует следующего согласования. Не обещайте доступ к внешним '
                          'сервисам, если он не подтверждён. Учитывайте все правки владельца. Ответ на русском.\n' + intake['instructions'])
                result = await complete(provider, [{'role': 'system', 'content': system}, {'role': 'user', 'content':
                    json.dumps({'idea': intake['original_text'], 'feedback': intake['feedback']}, ensure_ascii=False)}],
                    temperature=.3, max_tokens=reserved)
                self._settle(task_id, result['usage'])
                settled = True
                if result.get('error'):
                    raise EngineError(result['error'])
                proposal = _text(result.get('text'), 'Предложение', 10000)
                def finish(current):
                    pending = current['intake']
                    if current['status'] == 'review' and pending['state'] == 'analyzing' and pending['revision'] == revision:
                        pending.update(state='review', proposal=proposal, error='')
                        current['error'] = ''
                    return current
                db.mutate('task', task_id, finish)
                db.event(task_id, 'Ответ приёмного агента сохранён. Для запуска требуется одобрение владельца.')
        except asyncio.CancelledError:
            # Keep durable reservation. Recovery does not replay a paid request.
            raise
        except Exception as exc:
            if reserved and not settled:
                self._settle(task_id, {'input_tokens': 0, 'output_tokens': reserved, 'source': 'failed_request_upper_bound'})
            message = str(exc) if isinstance(exc, (EngineError, ProviderError)) else 'Ошибка анализа. Подробности скрыты для защиты секретов.'
            def fail(current):
                intake = current['intake']
                if current['status'] == 'review' and intake['state'] in {'queued', 'analyzing'} and intake['revision'] == revision:
                    intake.update(state='failed', error=message)
                    current['error'] = message
                return current
            db.mutate('task', task_id, fail)
            db.event(task_id, message, 'error')

    def _settle(self, task_id, usage):
        def change(task):
            if task['intake'].get('reserved_tokens'):
                self.engine._charge(task, usage)
                task['intake']['reserved_tokens'] = 0
            return task
        db.mutate('task', task_id, change)
