/* DOM integration test against a disposable DEMO_MODE=true server.
   No external accounts, publishing or real provider keys are used.
   UI_TEST_AUTH_FILE must point to {username,password} for that test server.
   This is a DOM test, not a visual browser verification. */
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {JSDOM,VirtualConsole}=require('jsdom');
const base=process.env.UI_TEST_URL||'http://localhost:8000';
const credentials=JSON.parse(fs.readFileSync(process.env.UI_TEST_AUTH_FILE,'utf8'));
const root=path.resolve(__dirname,'../app/static');
const diagnostics=[];
const vc=new VirtualConsole();vc.on('jsdomError',e=>diagnostics.push(e.message));
const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:base,runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:vc});
const w=dom.window;
w.structuredClone=structuredClone;
w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'))};
w.navigator.clipboard={writeText:async()=>{}};
let cookie='';let csrf='';
let fetchFixture=null;
const reply=data=>new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});
function deferred(){let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};}
// Isolated Telegram/intake HTTP fixtures: never start a real poller or send messages.
let intakeFixture=null;const intakeWrites=[];
w.fetch=async(url,options={})=>{
 if(fetchFixture){const response=await fetchFixture(String(url),options);if(response)return response;}
 if(intakeFixture){
  const method=options.method||'GET';const route=String(url);const body=options.body?JSON.parse(options.body):{};
  const reply=data=>new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});
  if(route==='/api/connectors'&&method==='POST'){intakeWrites.push({route,body});intakeFixture.connector={id:'ui-telegram',...body};return reply(intakeFixture.connector);}
  if(route==='/api/ideas'&&method==='POST'){intakeWrites.push({route,body});intakeFixture.task={id:'ui-idea',title:body.text,brief:body.text,status:'review',source:'web',steps:[],intake:{state:'review',revision:1,proposal:'Проверяемое предложение по идее'}};return reply(intakeFixture.task);}
  if(route.startsWith('/api/tasks/ui-idea/intake/')){intakeWrites.push({route,body});assert.equal(body.revision,intakeFixture.task.intake.revision);if(route.endsWith('/revise')){intakeFixture.task.intake.revision++;intakeFixture.task.intake.proposal='Предложение после правок';}else if(route.endsWith('/approve')){intakeFixture.task.intake.state='approved';intakeFixture.task.status='planning';}else{intakeFixture.task.intake.state='rejected';intakeFixture.task.status='cancelled';}return reply(intakeFixture.task);}
  if(route==='/api/tasks/ui-idea')return reply({task:intakeFixture.task,events:[]});
 }

 const headers=new Headers(options.headers||{});headers.set('Origin',base);
 if(cookie)headers.set('Cookie',cookie);
 const response=await fetch(new URL(url,base),{...options,headers});
 for(const value of response.headers.getSetCookie())if(value.startsWith('agentos_session='))cookie=value.split(';')[0];
 if(intakeFixture&&String(url)==='/api/state'){
  const data=await response.json();if(intakeFixture.connector)data.connectors.push(intakeFixture.connector);if(intakeFixture.task)data.tasks.push(intakeFixture.task);
  return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});
 }
 return response;
};
function q(sel){const n=w.document.querySelector(sel);assert(n,`Missing ${sel}`);return n;}
function byText(tag,text,scope=w.document){const n=[...scope.querySelectorAll(tag)].find(n=>n.textContent.trim()===text);assert(n,`Missing ${tag}: ${text}`);return n;}
function set(name,value){const n=q(`[name="${name}"]`);n.value=value;n.dispatchEvent(new w.Event('input',{bubbles:true}));n.dispatchEvent(new w.Event('change',{bubbles:true}));}
async function until(fn,label,timeout=10000){const end=Date.now()+timeout;while(Date.now()<end){if(fn())return;await new Promise(r=>setTimeout(r,20));}throw Error('Timeout: '+label);}
function submit(){const f=q('dialog form');f.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));}
function close(){if(q('dialog').open){const btn=[...q('dialog').querySelectorAll('button')].find(b=>/Закрыть/.test(b.getAttribute('aria-label')||''));if(btn)btn.click();else q('dialog').close();}}
async function nav(text){close();q(`nav button[aria-label="${text}"]`).click();await new Promise(r=>setTimeout(r,20));}
async function state(){return (await w.fetch('/api/state')).json();}
async function main(){
 w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+'\n'+fs.readFileSync(path.join(root,'workspace.js'),'utf8')+'\nwindow.__uiTest={S,refresh,openTask,openChat,refreshChat,renderApprovals,confirmPublication};');
 await until(()=>w.document.querySelector('[name=username]'),'login');
 set('username',credentials.username);set('password',credentials.password);
 q('form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 await until(()=>w.document.querySelector('nav'),'authenticated workspace');
 const initial=await state();assert.equal(initial.settings.demo_mode,true,'Use a disposable demo server only');
 assert(initial.providers.some(p=>p.kind==='mock'));
 byText('button','Новая задача').click();
 set('title','UI smoke '+Date.now());set('brief','Подготовь черновик поста. Не выдумывай источники и факты.');
 const taskTitle=q('[name=title]').value;
 assert(q('[name=planner_provider_id]').value,'Single provider should be selected');
 submit();
 await until(()=>w.document.querySelector('dialog h2')?.textContent===taskTitle,'task detail');
 await until(()=>[...w.document.querySelectorAll('button')].some(b=>b.textContent.trim()==='Запустить'),'ready plan');
 byText('button','Изменить план',q('dialog')).click();
 assert(q('dialog').textContent.includes('Зависит от этапов'), JSON.stringify({dialog:q('dialog').textContent,diagnostics}));
 submit();
 await until(()=>[...q('dialog').querySelectorAll('button')].some(b=>b.textContent.trim()==='Запустить'),'saved graph');
 byText('button','Запустить',q('dialog')).click();
 await until(()=>q('dialog').textContent.includes('Завершена'),'task completed');
 byText('button','Результат',q('dialog')).click();
 assert(q('dialog pre').textContent.length>30,'Actual demo output is visible');
 assert(q('dialog a[download]').getAttribute('href').endsWith('/export'),'Task report is downloadable');
 await nav('Агенты');
 byText('button','Создать агента').click();
 set('name','UI редактор');set('role','Редактор');set('instructions','Исправляй стиль. Отмечай неподтверждённые факты.');
 const provider=(await state()).providers.find(p=>p.kind==='mock');
 set('provider_id',provider.id);submit();
 await until(()=>!q('dialog').open,'agent saved');
 assert((await state()).agents.some(a=>a.name==='UI редактор'));
 await nav('Модели и API');
 byText('button','Тест API').click();
 await until(()=>q('dialog').textContent.includes('Результат проверки'),'provider check');
 await nav('Интеграции');
 byText('button','Подключить канал').click();set('name','UI webhook · no send');set('kind','webhook');
 set('config_url','https://example.com/agentos-ui-test');set('secret_bearer_token','disposable-fixture-secret');submit();
 await until(()=>!q('dialog').open,'connector saved');
 assert((await state()).connectors.some(c=>c.kind==='webhook'));
 await nav('Публикации');byText('button','Новый черновик').click();
 set('title','UI draft');set('text','Только черновик. Этот тест ничего не публикует.');submit();
 await until(()=>!q('dialog').open,'publication draft saved');
 assert((await state()).publications.some(p=>p.title==='UI draft'&&p.status==='draft'));
 // Exercise Telegram form persistence and shared intake approval with local HTTP fixtures.
 intakeFixture={};
 await nav('Интеграции');byText('button','Подключить канал').click();
 set('name','UI Telegram');set('config_allowed_user_ids','123456789');set('secret_bot_token','fixture-token-not-sent');set('config_planner_provider_id',provider.id);set('config_intake_instructions','Сначала уточни цель');
 set('kind','webhook');set('kind','telegram');
 assert.equal(q('[name=config_allowed_user_ids]').value,'123456789');assert.equal(q('[name=secret_bot_token]').value,'fixture-token-not-sent');assert.equal(q('[name=config_intake_instructions]').value,'Сначала уточни цель');assert.equal(q('[name=config_planner_provider_id]').value,provider.id);
 submit();await until(()=>!q('dialog').open,'Telegram settings saved');
 assert.equal(intakeFixture.connector.config.controller_enabled,true);assert.equal(intakeFixture.connector.config.voice_enabled,false);assert.deepEqual(intakeFixture.connector.config.allowed_user_ids,[123456789]);
 await nav('Задачи');byText('button','Новая идея').click();set('text','Идея из браузера');submit();
 await until(()=>q('dialog').textContent.includes('Проверяемое предложение по идее'),'intake proposal');
 assert(![...q('dialog').querySelectorAll('button')].some(b=>['Спланировать','Запустить','Изменить план'].includes(b.textContent.trim())),'No approval bypass in pending intake UI');
 byText('button','Внести правки',q('dialog')).click();set('feedback','Добавь план проверки');submit();
 await until(()=>q('dialog').textContent.includes('Предложение после правок'),'revised proposal');
 assert.equal(intakeWrites.find(x=>x.route.endsWith('/revise')).body.feedback,'Добавь план проверки');
 byText('button','Одобрить и выполнить',q('dialog')).click();byText('button','Одобрить и выполнить',q('dialog')).click();
 await until(()=>q('dialog').textContent.includes('Предложение одобрено'),'intake approved');assert.equal(intakeWrites.find(x=>x.route.endsWith('/approve')).body.revision,2);
 await nav('Настройки');await until(()=>w.document.getElementById('totp-settings')?.textContent.includes('Настроить 2FA'),'security settings');
 // New workspace features use the real isolated API, not fixture responses.
 intakeFixture=null;
 await nav('Навыки и маски');byText('button','Добавить навык').click();set('name','Тестовый навык');set('instructions','Проверяй исходные данные.');submit();
 await until(()=>!q('dialog').open,'skill saved');
 await nav('Проекты');byText('button','Создать проект').click();set('name','Русская редакция');set('description','Контент и отчёты');set('planner_provider_id',initial.providers[0].id);submit();
 await until(()=>!q('dialog').open,'project saved');
 await nav('Чаты');byText('button','Новый чат').click();set('title','Диалог');set('provider_id',initial.providers[0].id);submit();
 await until(()=>w.document.querySelector('#chat-input'),'chat open');
 q('#chat-input').value='Привет';q('.chat-composer').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 await until(()=>q('#chat-messages').textContent.includes('ДЕМОНСТРАЦИЯ'),'chat response');
 await nav('MCP-серверы');byText('button','Подключить MCP').click();set('name','Тестовый MCP');set('url','https://example.com/mcp');submit();
 await until(()=>!q('dialog').open,'MCP saved without network');
 await nav('Автоматизации');byText('button','Создать автоматизацию').click();set('name','Сводка');set('project_id',(await state()).projects[0].id);set('brief','Подготовь сводку');submit();
 await until(()=>!q('dialog').open,'automation saved disabled');
 await nav('Согласования');assert(w.document.body.textContent.includes('Сейчас согласований нет'));
 await nav('CRM и система');await until(()=>w.document.body.textContent.includes('Агентная 2.0'),'system status');
 await checkAsyncRegressions();
 assert(!diagnostics.length,diagnostics.join('\n'));
 console.log('PASS: login, task lifecycle/export, Russian workspace forms, Telegram intake, chat draft persistence, stale-response isolation, duplicate-submit protection, publication revision header, MCP approval states. No external publishing.');
 dom.window.close();
}
async function checkAsyncRegressions(){
 const ui=w.__uiTest;
 // State polling must preserve the same form controls and the user's draft.
 await nav('Навыки и маски');byText('button','Добавить навык').click();set('name','Несохранённый навык');set('instructions','Черновик инструкций');
 const draft=q('[name=instructions]');draft.focus();await until(()=>!ui.S.loading,'idle poll');await ui.refresh(true);
 assert.equal(q('[name=instructions]'),draft);assert.equal(draft.value,'Черновик инструкций');assert.equal(w.document.activeElement,draft);
 // A slow save must execute once and must not close a new form opened meanwhile.
 const saved=deferred();let writes=0;
 fetchFixture=(route,o)=>{if(route==='/api/skills'&&o.method==='POST'){writes++;return saved.promise;}};
 const oldForm=q('dialog form');submit();submit();await until(()=>writes===1,'one skill request');
 await nav('MCP-серверы');byText('button','Подключить MCP').click();set('name','Черновик подключения');
 saved.resolve(reply({id:'saved-fixture'}));await until(()=>!oldForm.hasAttribute('aria-busy'),'pending form saved');
 assert.equal(writes,1);assert(q('dialog').open);assert.equal(q('[name=name]').value,'Черновик подключения');fetchFixture=null;
 // Details for a previous task must not replace the newly opened task.
 close();const first=(await state()).tasks.find(t=>t.steps?.length);await ui.openTask(first.id);await until(()=>!ui.S.loading,'idle task poll');
 const late=deferred();let requested=false;const second={...first,id:'ui-second-task',title:'Другая открытая задача'};
 fetchFixture=route=>{if(route===`/api/tasks/${first.id}`){requested=true;return late.promise;}if(route==='/api/tasks/ui-second-task')return reply({task:second,events:[]});};
 const poll=ui.refresh(true);await until(()=>requested,'delayed task detail');await ui.openTask(second.id);
 late.resolve(reply({task:{...first,title:'Устаревший ответ'},events:[]}));await poll;
 assert.equal(q('dialog h2').textContent,second.title);assert.equal(ui.S.taskDetail.task.id,second.id);fetchFixture=null;
 // A response arriving after the user opened a form must not reopen its task.
 close();const opening=deferred();fetchFixture=route=>route===`/api/tasks/${first.id}`?opening.promise:undefined;
 const pendingOpen=ui.openTask(first.id);await nav('Навыки и маски');byText('button','Добавить навык').click();set('name','Другой черновик');
 opening.resolve(reply({task:first,events:[]}));await pendingOpen;
 assert.equal(q('[name=name]').value,'Другой черновик');fetchFixture=null;
 // Sending a chat message must not erase text entered during a slow request.
 close();const sent=deferred();let messages=0;let heldRead=null;let fixtureChat={id:'ui-chat-race',title:'Проверка диалога',status:'idle',messages:[]};
 fetchFixture=(route,o)=>{if(route==='/api/chats/ui-chat-race')return heldRead?heldRead.promise:reply(fixtureChat);if(route==='/api/chats/ui-chat-race/messages'&&o.method==='POST'){messages++;return sent.promise;}};
 await ui.openChat(fixtureChat.id);const staleChat=fixtureChat;heldRead=deferred();const earlierPoll=ui.refreshChat();
 q('#chat-input').value='Первое сообщение';q('.chat-composer').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));q('.chat-composer').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 q('#chat-input').value='Следующий черновик';fixtureChat={...fixtureChat,status:'running',messages:[{role:'user',content:'<img src=x onerror=alert(1)>'}]};sent.resolve(reply(fixtureChat));
 await until(()=>!ui.S.modal.sending,'chat accepted');heldRead.resolve(reply(staleChat));await earlierPoll;heldRead=null;
 assert.equal(messages,1);assert.equal(q('#chat-input').value,'Следующий черновик');assert(q('#chat-send').disabled);assert(!q('#chat-messages').querySelector('img'));assert(q('#chat-messages').textContent.includes('<img'));
 assert.equal(q('#chat-input').getAttribute('aria-label'),'Сообщение агенту');
 fixtureChat={...fixtureChat,status:'idle',tokens_used:17,usage_uncertain:true};await ui.refreshChat();assert(!q('#chat-send').disabled);assert(q('#chat-stop').disabled);
 assert(q('#chat-usage').textContent.includes('17'));assert(q('#chat-usage').textContent.includes('Расход токенов может быть неполным'));
 fixtureChat={...fixtureChat,usage_uncertain:false};await ui.refreshChat();assert(!q('#chat-usage').textContent.includes('неполным'));fetchFixture=null;close();
 // During a JEV check, rejection remains possible but approval is unavailable.
 const requests=ui.S.data.tool_requests;ui.S.data.tool_requests=[{id:'checking-fixture',status:'checking',tool:'example',arguments:{}}];const approvals=w.document.createElement('main');ui.renderApprovals(approvals);
 assert([...approvals.querySelectorAll('button')].some(b=>b.textContent==='Отклонить'));assert(![...approvals.querySelectorAll('button')].some(b=>b.textContent==='Одобрить и выполнить'));ui.S.data.tool_requests=requests;
 // Publication approval is bound to the exact visible revision; no publishing.
 const publication=(await state()).publications.find(p=>p.status==='draft');let revision=null;
 fetchFixture=(route,o)=>{if(route===`/api/publications/${publication.id}/approve`){revision=o.headers['X-Resource-Version'];return reply({});}};
 ui.confirmPublication(publication,'approve');byText('button','Одобрить',q('dialog')).click();await until(()=>!q('dialog').open,'approval dialog closed');assert.equal(revision,publication.updated_at);fetchFixture=null;
}
main().catch(e=>{console.error(e.stack);dom.window.close();process.exitCode=1;});
