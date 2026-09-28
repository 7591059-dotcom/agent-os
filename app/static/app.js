'use strict';

const app = document.getElementById('app');
const modal = document.getElementById('modal');
const S = {view:'tasks', data:{tasks:[],agents:[],providers:[],connectors:[],events:[],publications:[],settings:{}}, session:{}, search:'', mode:'board', online:true, modal:null, modalRequest:0, taskDetail:null, signature:'', loading:false};
const views = {projects:['Проекты','graph'], chats:['Чаты','file'], skills:['Навыки и маски','file'], mcp:['MCP-серверы','plug'], approvals:['Согласования','shield'], automations:['Автоматизации','clock'], system:['CRM и система','server'], tasks:['Задачи','board'], agents:['Агенты','agents'], providers:['Модели и API','cpu'], integrations:['Интеграции','plug'], content:['Публикации','send'], settings:['Настройки','settings']};
const statuses = {backlog:'Новая',planning:'Планирование',ready:'Готова к запуску',running:'В работе',paused:'На паузе',review:'На проверке',done:'Завершена',failed:'Ошибка',cancelled:'Отменена',pending:'Ожидает',draft:'Черновик',approved:'Одобрено',rejected:'Отклонено',publishing:'Публикуется',published:'Опубликовано',uncertain:'Нужна проверка'};
const providerKinds = {openai:'OpenAI',anthropic:'Anthropic',gemini:'Google Gemini',openai_compatible:'Совместимый с OpenAI API',mock:'Демо · тестовая модель'};
const connectorKinds = {telegram:'Telegram',vk:'ВКонтакте',instagram:'Instagram',youtube:'YouTube',webhook:'Webhook'};
const actionNames = {plan:'Спланировать',run:'Запустить',pause:'Пауза',resume:'Продолжить',cancel:'Отменить задачу',retry:'Повторить'};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key,value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2),value);
    else if (key === 'text') node.textContent = value;
    else if (key === 'checked' || key === 'disabled' || key === 'selected' || key === 'required') node[key] = Boolean(value);
    else if (key === 'value') node.value = value;
    else node.setAttribute(key,value === true ? '' : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
function setChildren(node,...children){node.replaceChildren(...children.flat(Infinity).filter(x=>x!==null&&x!==undefined&&x!==false));}
function icon(name,size=18) {
  const paths = {
    board:['M3 4h18v16H3z','M9 4v16M15 4v16M6 8v4M12 8v7M18 8v3'],
    list:['M8 6h13M8 12h13M8 18h13','M3 6h.01M3 12h.01M3 18h.01'],
    agents:['M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2','M16 3a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.87'],
    cpu:['M6 6h12v12H6zM9 9h6v6H9z','M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4'],
    plug:['m12 3 2 2-7 7-2-2zM14 5l4-4M10 9l4 4M3 14l7 7M1 23l5-5M7 10l7 7M5 12l7 7 6-6-7-7z'],
    send:['m22 2-7 20-4-9-9-4zM22 2 11 13'],
    settings:['m12 3 2 3 4 .2.8 3.8L22 12l-3.2 2  -.8 3.8-4 .2-2 3-2-3-4-.2L5.2 14 2 12l3.2-2L6 6.2l4-.2z'],
    plus:['M12 5v14M5 12h14'],search:['m21 21-4.4-4.4'],check:['m5 12 4 4L19 6'],
    close:['m6 6 12 12M6 18 18 6'],arrow:['M4 12h15m-5-5 5 5-5 5'],chevron:['m9 5 7 7-7 7'],
    play:['m8 4 12 8-12 8z'],pause:['M8 5v14M16 5v14'],stop:['M5 5h14v14H5z'],
    refresh:['M20 7v5h-5M4 17v-5h5','M6 6a8 8 0 0 1 14 6M18 18A8 8 0 0 1 4 12'],
    clock:['M12 7v5l3 2'],shield:['m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z','m8 12 3 3 5-6'],
    logout:['M9 4H4v16h5M9 12h12m-4-4 4 4-4 4'],
    server:['M3 3h18v7H3zM3 14h18v7H3z','M7 6.5h.01M7 17.5h.01M12 6.5h6M12 17.5h6'],
    activity:['M2 12h5l3-8 4 16 3-8h5'],lightning:['m13 2-9 12h7l-1 8 10-13h-7z'],
    edit:['m16 3 5 5-12 12-6 1 1-6zM13 6l5 5'],trash:['M3 6h18M8 6V3h8v3M5 6l1 15h12l1-15M10 10v7M14 10v7'],
    key:['M16 12h6v4h-3v3h-4v-4l-4-4'],copy:['M8 8h13v13H8zM16 8V3H3v13h5'],
    file:['M14 2H4v20h16V8zM14 2v6h6M8 12h8M8 16h6'],
    link:['M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2'],
    menu:['M3 6h18M3 12h18M3 18h18'],info:['M12 11v6M12 7h.01'],
    graph:['M4 5h5v5H4zM15 3h5v5h-5zM15 16h5v5h-5zM9 7.5h3V5.5h3M12 7.5v11H15'],
    inbox:['M4 4h16l2 13v4H2v-4zM2 15h6l2 3h4l2-3h6'],
    eye:['M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z'],
    lock:['M5 10h14v11H5zM8 10V6a4 4 0 0 1 8 0v4M12 14v3']
  };
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  for (const [k,v] of Object.entries({width:size,height:size,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.65','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'})) svg.setAttribute(k,v);
  for (const d of paths[name] || paths.file) { const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',d);svg.append(p); }
  const circles={agents:[9,7,4],search:[10.5,10.5,6.5],clock:[12,12,9],settings:[12,12,3],info:[12,12,9],eye:[12,12,3],key:[7,7,5]};
  if(circles[name]) {const c=document.createElementNS(svg.namespaceURI,'circle');['cx','cy','r'].forEach((k,i)=>c.setAttribute(k,circles[name][i]));svg.append(c);}
  return svg;
}
function button(label, fn, cls='', ico=null, attrs={}) {return el('button',{type:'button',class:`btn ${cls}`,onclick:fn,...attrs},ico?icon(ico,16):null,label);}
function iconButton(label,ico,fn) {return el('button',{type:'button',class:'icon-btn',title:label,'aria-label':label,onclick:fn},icon(ico,17));}
function badge(status) {return el('span',{class:`badge ${status}`},statuses[status] || status);}
function initials(name='') {return String(name).split(/\s+/).slice(0,2).map(p=>p[0]||'').join('').toUpperCase() || 'А';}
function shortId(id='') {return String(id).slice(0,6).toUpperCase();}
function date(value,withDate=false) {if(!value)return '—';const d=new Date(value);return Number.isNaN(d.getTime())?'—':d.toLocaleString('ru-RU',withDate?{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}:{hour:'2-digit',minute:'2-digit'});}
function count(n) {return Number(n||0).toLocaleString('ru-RU');}
function lookup(kind,id) {return S.data[kind].find(i=>i.id===id);}
function errorText(err) {return err instanceof Error?err.message:String(err);}
function toast(message,error=false) {const t=el('div',{class:`toast${error?' error':''}`},icon(error?'info':'check',17),el('span',{},message));document.getElementById('toast-region').append(t);setTimeout(()=>t.remove(),error?9000:5000);}
async function api(path,method='GET',data,extraHeaders={}) {
  const headers={Accept:'application/json',...extraHeaders};
  if(data!==undefined)headers['Content-Type']='application/json';
  if(method!=='GET')headers['X-CSRF-Token']=S.session.csrf_token||'';
  let response;
  try{response=await fetch(`/api${path}`,{method,headers,credentials:'same-origin',body:data===undefined?undefined:JSON.stringify(data)});}catch(e){throw new Error('Нет связи с сервером. Проверьте подключение и повторите попытку.');}
  let body;try{body=await response.json();}catch{body={};}
  if(!response.ok){
    if(response.status===401 && !path.startsWith('/auth/')){S.session.authenticated=false;closeModal();renderLogin();}
    let detail=body.detail||body.error||`Ошибка сервера (${response.status})`;
    if(Array.isArray(detail))detail=detail.map(x=>x.msg||String(x)).join('; ');
    if(typeof detail!=='string')detail=JSON.stringify(detail);
    throw new Error(detail);
  }
  return body;
}
async function refresh(force=false) {
  if(!S.session.authenticated || S.loading)return;
  S.loading=true;
  try {
    const data=await api('/state');
    for(const key of ['tasks','agents','providers','connectors','events','publications','projects','skills','masks','mcp_servers','tool_requests','chats','automations'])if(!Array.isArray(data[key]))data[key]=[];
    S.data={...data,settings:data.settings||{}}; S.online=true;
    const sig=JSON.stringify(data);
    if(force||sig!==S.signature){S.signature=sig;renderShell();}
    else updateConnection();
    if(S.modal?.type==='chat')await refreshChat();
    const currentModal=S.modal;
    if(currentModal?.type==='task'&&!currentModal.editing){const detail=await api(`/tasks/${currentModal.id}`);await ensureTaskResources(detail.task);if(S.modal===currentModal&&JSON.stringify(detail)!==JSON.stringify(S.taskDetail)){S.taskDetail=detail;renderTaskDetail();}}
  } catch(err){S.online=false;updateConnection();if(force)toast(errorText(err),true);}
  finally{S.loading=false;}
}
function updateConnection(){const n=document.getElementById('connection');if(n){n.classList.toggle('offline',!S.online);n.textContent=S.online?'Система на связи':'Нет связи с сервером';}}
function navigate(view){S.view=views[view]?view:'tasks';S.search='';location.hash=S.view;renderShell();}
function brand(cls=''){return el('div',{class:`brand ${cls}`},el('span',{class:'brand-icon'},icon('graph',29)),'АГЕНТНАЯ');}
function renderShell(){
  if(!S.session.authenticated)return;
  const sidebar=el('aside',{class:'sidebar',id:'sidebar'},brand(),el('div',{class:'workspace-label'},el('span',{class:'workspace-dot'}),'Личная рабочая область'),el('div',{class:'nav-label'},'УПРАВЛЕНИЕ'));
  const nav=el('nav',{class:'nav','aria-label':'Главная навигация'});
  for(const [id,[title,ico]] of Object.entries(views)){const n=el('button',{class:`nav-btn${S.view===id?' active':''}`,type:'button',onclick:()=>navigate(id),'aria-label':title,'aria-current':S.view===id?'page':null},icon(ico,18),title);if(id==='tasks')n.append(el('span',{class:'nav-count'},S.data.tasks.length));if(id==='agents'&&S.data.agents.length)n.append(el('span',{class:'nav-count'},S.data.agents.length));nav.append(n);}
  sidebar.append(nav,el('div',{class:'sidebar-bottom'},el('div',{class:'host-card'},icon('server',18),el('div',{},el('strong',{},'Ваш сервер. Ваши агенты.'),'Собственная рабочая среда')),el('button',{class:'user-btn',onclick:()=>navigate('settings')},el('span',{class:'user-avatar'},initials(typeof S.session.user==='string'?S.session.user:S.session.user?.username||'Владелец')),el('span',{},el('strong',{},typeof S.session.user==='string'?S.session.user:S.session.user?.username||'Владелец'),'Администратор'),icon('settings',16))));
  const scrim=el('div',{class:'scrim',id:'scrim',onclick:()=>{sidebar.classList.remove('mobile-open');scrim.classList.remove('show');}});
  const topbar=el('header',{class:'topbar'},el('div',{class:'breadcrumb'},el('button',{class:'icon-btn mobile-menu','aria-label':'Открыть меню',onclick:()=>{sidebar.classList.toggle('mobile-open');scrim.classList.toggle('show');}},icon('menu')),
    el('span',{},'Рабочая область'),el('span',{class:'slash'},'/'),el('strong',{},views[S.view][0])),el('div',{class:'topbar-right'},S.data.settings.demo_mode||S.session.demo_mode?el('span',{class:'badge demo'},'ДЕМО-РЕЖИМ'):null,el('span',{class:`connection${S.online?'':' offline'}`,id:'connection'},S.online?'Система на связи':'Нет связи с сервером'),el('span',{class:'top-date'},new Date().toLocaleDateString('ru-RU',{day:'numeric',month:'long'}))));
  const main=el('main',{class:'main',id:'main'});
  const renderers={projects:renderProjects,chats:renderChats,skills:renderSkills,mcp:renderMCP,approvals:renderApprovals,automations:renderAutomations,system:renderSystem,tasks:renderTasks,agents:renderAgents,providers:renderProviders,integrations:renderIntegrations,content:renderContent,settings:renderSettings};
  renderers[S.view](main);
  const active=document.activeElement;const preserve=active?.id==='task-search';const cursor=preserve?active.selectionStart:null;
  setChildren(app,el('div',{class:'app-shell'},sidebar,scrim,el('div',{class:'main-shell'},topbar,main)));
  if(preserve){const input=document.getElementById('task-search');input?.focus();input?.setSelectionRange(cursor,cursor);}
}
function pageHead(title,desc,action){return el('div',{class:'page-head'},el('div',{},el('h1',{},title),el('p',{class:'page-desc'},desc)),action);}
function emptyState(ico,title,desc,action){return el('div',{class:'empty-state'},icon(ico,33),el('h2',{},title),el('p',{},desc),action);}
function notice(text,warning=false){return el('div',{class:`notice${warning?' warning':''}`},icon('info',18),el('span',{},text));}
function renderTasks(main){
  main.append(pageHead('Центр управления','Ставьте задачи. Собирайте команды агентов. Следите за результатом.',el('div',{class:'toolbar-actions'},button('Новая задача',()=>taskForm(),'ghost','plus'),button('Новая идея',()=>ideaForm(),'primary','lightning'))));
  const tasks=S.data.tasks;
  const stats=[['Всего задач',tasks.length,'board','в рабочей области'],['Сейчас в работе',tasks.filter(t=>['running','planning'].includes(t.status)).length,'activity','активные процессы'],['Ждут внимания',tasks.filter(t=>['ready','paused','review','failed'].includes(t.status)).length,'eye','нужен ваш следующий шаг'],['Активных агентов',S.data.agents.filter(a=>a.enabled!==false&&lookup('providers',a.provider_id)?.enabled!==false&&lookup('providers',a.provider_id)).length,'agents','готовы к работе']];
  main.append(el('div',{class:'stats'},stats.map(([label,n,ico,note])=>el('div',{class:'stat'},el('div',{class:'stat-label'},icon(ico,15),label),el('div',{class:'stat-value'},count(n)),el('span',{class:'stat-note'},note)))));
  if(!S.data.providers.some(p=>p.enabled!==false))main.append(el('div',{class:'setup-strip'},el('div',{class:'setup-symbol'},icon('cpu',20)),el('div',{},el('h3',{},'Подключите первую модель'),el('p',{},'Добавьте API-ключ провайдера — оркестратор сможет создать команду под вашу задачу.')),button('Подключить модель',()=>providerForm(),'small','arrow')));
  const search=el('input',{id:'task-search',placeholder:'Найти задачу…',value:S.search,'aria-label':'Поиск задач',oninput:e=>{S.search=e.target.value;renderTaskCollection(document.getElementById('task-collection'));}});
  main.append(el('div',{class:'section-toolbar'},el('div',{class:'segmented'},buttonSegment('Доска','board','board'),buttonSegment('Список','list','list')),el('div',{class:'toolbar-actions'},el('label',{class:'search'},icon('search',16),search),iconButton('Обновить','refresh',()=>refresh(true)))));
  main.append(field('Проект',select('project-filter',[['','Все проекты'],...S.data.projects.map(p=>[p.id,p.name])],S.projectId||'',{onchange:e=>{S.projectId=e.target.value;renderTaskCollection(document.getElementById('task-collection'));}})));
  const collection=el('div',{id:'task-collection'});renderTaskCollection(collection);main.append(collection);
  main.append(el('div',{class:'board-footer'},el('span',{},icon('graph',13),'Статусы обновляются по ходу выполнения'),el('span',{},icon('refresh',12),'Автообновление · каждые 3 секунды')));
  const events=S.data.events.slice().sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at))).slice(0,4);
  main.append(el('section',{class:'activity-section'},el('div',{class:'section-title'},el('h2',{},'Последние события'),el('span',{class:'eyebrow'},'Журнал системы')),events.length?eventList(events):el('p',{class:'empty-inline'},'Здесь появятся действия оркестратора, запуски агентов и результаты задач.')));
}
function buttonSegment(label,mode,ico){return el('button',{class:`segment${S.mode===mode?' active':''}`,onclick:()=>{S.mode=mode;renderShell();},'aria-pressed':S.mode===mode},icon(ico,14),label);}
function renderTaskCollection(container){
  if(!container)return;const q=S.search.toLowerCase();const tasks=S.data.tasks.filter(t=>(!S.projectId||t.project_id===S.projectId)&&`${t.title} ${t.brief} ${t.id}`.toLowerCase().includes(q));
  if(S.mode==='list'){
    if(!tasks.length){setChildren(container,emptyState('inbox',q?'Ничего не найдено':'Пока нет задач',q?'Попробуйте другое название или очистите поиск.':'Опишите цель. Оркестратор предложит этапы и создаст нужных агентов.',!q?button('Создать задачу',()=>taskForm(),'primary','plus'):null));return;}
    setChildren(container,el('div',{class:'table-wrap'},el('table',{},el('thead',{},el('tr',{},['Задача','Статус','Этапы','Токены','Обновлена'].map(t=>el('th',{},t)))),el('tbody',{},tasks.map(t=>el('tr',{},el('td',{},el('button',{class:'table-action',onclick:()=>openTask(t.id)},el('strong',{},t.title),el('small',{class:'muted'},`#${shortId(t.id)}`))),el('td',{},badge(t.status)),el('td',{},`${(t.steps||[]).filter(s=>s.status==='done').length} / ${(t.steps||[]).length}`),el('td',{},count(t.tokens_used)),el('td',{},date(t.updated_at,true))))))));return;
  }
  const groups=[['backlog','В очереди',['backlog','planning','ready'],'inbox','Начните с задачи','Опишите, чего хотите добиться. Оркестратор соберёт план.'],['running','В работе',['running'],'activity','Всё готово к запуску','Здесь будут задачи, над которыми сейчас работают агенты.'],['review','Внимание',['paused','review','failed'],'eye','Ничего не ждёт решения','Паузы, ошибки и результаты для вашей проверки.'],['done','Завершено',['done','cancelled'],'check','Здесь будут результаты','Готовые задачи и их история остаются под рукой.']];
  setChildren(container,el('div',{class:'board'},groups.map(([id,title,states,ico,emptyTitle,desc])=>{
    const group=tasks.filter(t=>states.includes(t.status));
    return el('section',{class:'kanban-column','aria-label':title},el('div',{class:'column-head'},el('span',{class:`column-dot ${id}`}),title,el('span',{class:'column-count'},group.length),id==='backlog'?iconButton('Добавить задачу','plus',()=>taskForm()):null),group.length?group.map(taskCard):el('div',{class:'column-empty'},el('div',{class:'empty-column-icon'},icon(ico,20)),el('strong',{},q?'Нет совпадений':emptyTitle),el('p',{},q?'В этой колонке ничего не найдено.':desc)));
  })));
}
function taskCard(t){const steps=t.steps||[];const done=steps.filter(s=>s.status==='done').length;const agents=[...new Set(steps.map(s=>s.agent_id))].map(id=>lookup('agents',id)).filter(Boolean);const bar=el('progress',{class:'card-progress',max:steps.length||1,value:done,'aria-label':`Готово этапов: ${done} из ${steps.length}`});return el('button',{class:'task-card',onclick:()=>openTask(t.id),'aria-label':`${t.title}, ${statuses[t.status]||t.status}`},el('div',{class:'task-card-top'},el('span',{class:'task-id mono'},`#${shortId(t.id)}`),badge(t.status)),el('h3',{},t.title),t.intake?el('p',{class:'field-help'},t.source==='telegram'?'Идея из Telegram':'Идея из веб-интерфейса'):null,el('p',{class:'task-brief'},t.brief||'Описание не добавлено'),steps.length?bar:null,el('div',{class:'card-meta'},el('div',{class:'avatar-stack'},agents.slice(0,3).map(a=>el('span',{class:'agent-avatar',title:a.name},initials(a.name))),!agents.length?el('span',{},'Агенты ещё не назначены'):null),el('span',{},steps.length?`${done}/${steps.length} этапов`:date(t.created_at))),t.error?el('p',{class:'field-help mt-10'},'Откройте задачу, чтобы проверить ошибку'):null);}
function eventList(events){return el('div',{class:'event-list'},events.map(ev=>el('div',{class:`event-row ${ev.level==='error'?'error':''}`},el('time',{class:'event-time',datetime:ev.created_at},date(ev.created_at)),el('span',{class:'event-dot'}),el('p',{},ev.message))));}

function renderAgents(main){
  main.append(pageHead('Команда агентов','У каждого — своя роль, инструкции и модель. Оркестратор может создавать агентов под задачу.',button('Создать агента',()=>agentForm(),'primary','plus')));
  if(!S.data.agents.length){main.append(emptyState('agents','Соберите свою команду','Создайте агента вручную или поручите это оркестратору при планировании задачи.',button('Создать агента',()=>agentForm(),'primary','plus')));return;}
  main.append(el('div',{class:'card-grid'},S.data.agents.map(a=>{const provider=lookup('providers',a.provider_id);const assigned=S.data.tasks.filter(t=>(t.steps||[]).some(s=>s.agent_id===a.id)).length;return el('article',{class:'resource-card'},el('div',{class:'resource-top'},el('div',{class:'resource-icon'},initials(a.name)),el('div',{},el('h3',{},a.name),el('p',{class:'resource-subtitle'},a.role||'Агент')),el('span',{class:'badge'},el('span',{class:`status-dot${a.enabled===false?' disabled':''}`}),a.enabled===false?'Выключен':'Активен')),el('p',{class:'resource-body'},a.instructions||'Инструкции не заданы'),el('div',{class:'resource-facts'},el('span',{class:'fact'},provider?.name||'Модель не найдена'),el('span',{class:'fact mono'},a.model||provider?.model||'Модель провайдера')),el('div',{class:'resource-footer'},el('span',{class:'muted text-11'},`Задач: ${assigned}`),el('div',{class:'toolbar-actions'},button('Настроить',()=>agentForm(a),'small','edit'),iconButton('Удалить агента','trash',()=>deleteResource('agents',a)))));})));}
function renderProviders(main){
  main.append(pageHead('Модели и API','Подключите провайдеров и назначайте разные модели оркестратору и агентам.',button('Добавить провайдера',()=>providerForm(),'primary','plus')));
  main.append(notice('Здесь используются ключи API. Доступность модели и тарификация определяются провайдером. Для подписки на чат-приложение проверяйте API-доступ отдельно. Кнопка «Тест API» отправляет короткий запрос, который может тарифицироваться.'));
  if(!S.data.providers.length){main.append(emptyState('cpu','Дайте агентам модель','Добавьте OpenAI, Anthropic, Gemini или провайдера с совместимым API. Ключ хранится на вашем сервере в зашифрованном виде.',button('Подключить API',()=>providerForm(),'primary','key')));return;}
  main.append(el('div',{class:'card-grid'},S.data.providers.map(p=>el('article',{class:'resource-card'},el('div',{class:'resource-top'},el('div',{class:'resource-icon'},icon('cpu',23)),el('div',{},el('h3',{},p.name),el('p',{class:'resource-subtitle'},providerKinds[p.kind]||p.kind)),el('span',{class:`badge ${p.kind==='mock'?'demo':''}`},p.kind==='mock'?'ДЕМО':p.enabled===false?'Выключен':'Настроен')),el('div',{class:'resource-facts'},el('span',{class:'fact mono'},p.model||'Модель не указана')),el('p',{class:'resource-body'},p.base_url||'Официальный API провайдера'),el('div',{class:'resource-footer'},button('Тест API',e=>testResource('providers',p,e.currentTarget),'small','activity',{title:'Короткий запрос к модели может тарифицироваться провайдером'}),el('div',{class:'toolbar-actions'},iconButton('Изменить провайдера','edit',()=>providerForm(p)),iconButton('Удалить провайдера','trash',()=>deleteResource('providers',p))))))));
}
function renderIntegrations(main){
  main.append(pageHead('Интеграции','Управление через Telegram и подготовка публикаций для ваших площадок.',button('Подключить канал',()=>connectorForm(),'primary','plus')));
  main.append(notice('Публикации отправляются только после вашего одобрения и отдельного запуска. Telegram принимает команды только от указанных пользователей в личном чате.'));
  if(!S.data.connectors.length){main.append(emptyState('plug','Подключите рабочие каналы','Telegram для управления с телефона. ВКонтакте, Instagram, YouTube и Webhook — для публикации подготовленного контента.',button('Добавить интеграцию',()=>connectorForm(),'primary','plus')));return;}
  main.append(el('div',{class:'card-grid'},S.data.connectors.map(c=>el('article',{class:'resource-card'},el('div',{class:'resource-top'},el('div',{class:'resource-icon'},c.kind==='telegram'?icon('send',23):c.kind==='webhook'?icon('link',23):(connectorKinds[c.kind]||c.kind).slice(0,2).toUpperCase()),el('div',{},el('h3',{},c.name),el('p',{class:'resource-subtitle'},connectorKinds[c.kind]||c.kind)),el('span',{class:'badge'},c.enabled===false?'Выключена':'Включена')),el('p',{class:'resource-body'},connectorSummary(c)),el('div',{class:'resource-footer'},button('Проверить',e=>testResource('connectors',c,e.currentTarget),'small','activity'),el('div',{class:'toolbar-actions'},iconButton('Настроить интеграцию','edit',()=>connectorForm(c)),iconButton('Удалить интеграцию','trash',()=>deleteResource('connectors',c))))))));
  if(S.data.connectors.some(c=>c.kind==='telegram'))main.append(el('section',{class:'settings-card mt-24'},el('h2',{},'Команды Telegram'),el('p',{},'Отправьте боту идею текстом или голосовым сообщением (если включено). Проверьте предложение, одобрите или отправьте правки. Компьютер с системой должен оставаться включённым. Команды: /help.'),el('div',{class:'flow-summary'},['/tasks','/task <id>','/pause <id>','/resume <id>','/cancel <id>','/result <id>','/status','/newchat'].map(c=>el('span',{class:'flow-chip mono'},c)))));
}
function connectorSummary(c){const cfg=c.config||{};switch(c.kind){case'telegram':return `${cfg.controller_enabled===false?'Только публикации':'Приём идей и управление'} · пользователей: ${(cfg.allowed_user_ids||[]).length}${cfg.chat_id?' · публикации подключены':''}${c.last_poll_at?' · проверка '+date(c.last_poll_at,true):''}${c.last_error?' · '+c.last_error:''}`;case'vk':return `Стена ${cfg.owner_id||'не указана'} · текстовые публикации`;case'instagram':return `Аккаунт ${cfg.account_id||'не указан'} · ${cfg.media_type==='REELS'?'Reels':'изображения'}`;case'youtube':return `Загрузка видео · ${cfg.privacy_status==='public'?'открытый доступ':cfg.privacy_status==='unlisted'?'доступ по ссылке':'приватный доступ'}`;default:return cfg.url||'HTTPS webhook';}}
function renderContent(main){
  main.append(pageHead('Публикации','От результата агента до готового поста. Вы проверяете контент перед отправкой.',button('Новый черновик',()=>publicationForm(),'primary','plus')));
  main.append(notice('Последовательность: черновик → одобрение → публикация. Одобрение само по себе не отправляет контент на площадку.'));
  if(!S.data.publications.length){main.append(emptyState('send','Контент начинается с черновика','Используйте результат задачи или добавьте собственный текст. Затем выберите подключённый канал и проверьте публикацию.',button('Создать черновик',()=>publicationForm(),'primary','plus')));return;}
  main.append(el('div',{class:'card-grid'},S.data.publications.map(p=>{const connector=lookup('connectors',p.connector_id);const footer=el('div',{class:'resource-footer'});if(['draft','failed','approved'].includes(p.status)){footer.append(button(p.status==='failed'?'Исправить':'Редактировать',()=>publicationForm(p),'small','edit'));if(p.status==='draft')footer.append(button('Одобрить',()=>confirmPublication(p,'approve'),'small primary','check'));}if(p.status==='approved')footer.append(button('Опубликовать',()=>confirmPublication(p,'publish'),'small primary','send'));if(p.status==='published')footer.append(el('span',{class:'muted text-12'},'Отправлено на площадку'));if(p.status==='uncertain')footer.append(el('span',{class:'muted text-12'},'Проверьте результат на площадке'));if(p.status==='publishing')footer.append(el('span',{class:'muted text-12'},'Публикация выполняется…'));if(p.status==='draft')footer.append(iconButton('Удалить черновик','trash',()=>deleteResource('publications',{...p,name:p.title||'Черновик'})));return el('article',{class:'resource-card'},el('div',{class:'resource-top'},el('div',{class:'resource-icon'},icon('file',22)),el('div',{},el('h3',{},p.title||'Публикация'),el('p',{class:'resource-subtitle'},connector?.name||'Канал не найден')),badge(p.status)),el('p',{class:'publication-content'},p.text||'Без текста'),p.media_url?el('p',{class:'publication-meta'},`Медиа: ${p.media_url}`):null,el('p',{class:'publication-meta'},date(p.created_at,true),p.external_id?` · ID: ${p.external_id}`:''),p.error?el('p',{class:'form-error mb-15'},p.error):null,footer);})));}
async function renderSettings(main){
  main.append(pageHead('Настройки','Доступ к системе и параметры вашей рабочей среды.'));
  const security=el('section',{class:'settings-card'},el('h2',{},'Защита входа'),el('p',{},'Вход по паролю владельца. Добавьте код из приложения-аутентификатора для второго фактора.'),el('div',{id:'totp-settings'},el('p',{class:'muted'},'Проверяем настройки…')));
  main.append(el('div',{class:'settings-grid'},el('div',{},security,el('section',{class:'settings-card'},el('h2',{},'Текущая сессия'),el('p',{},'Завершение сессии вернёт вас на страницу входа.'),button('Выйти из системы',async()=>{try{await api('/auth/logout','POST',{});S.session={};closeModal();renderLogin();}catch(e){toast(errorText(e),true);}},'ghost','logout'))),el('div',{},el('section',{class:'settings-card'},el('h2',{},'Рабочая среда'),settingRow('Режим',S.data.settings.demo_mode?'Демонстрационный':'Рабочий'),settingRow('Хранение','На вашем сервере'),settingRow('Публикации','После одобрения владельцем'),settingRow('Команды агентов','Без доступа к shell'),settingRow('Telegram',S.data.settings.telegram_configured?'Настроен':'Не подключён')),el('section',{class:'settings-card'},el('h2',{},'Развёртывание'),el('p',{},'HTTPS, пароль владельца, ключ шифрования и резервные копии настраиваются при установке на VPS. Инструкции находятся в комплекте проекта.'),el('p',{class:'muted'},'Пароль администратора изменяется в конфигурации сервера. После изменения перезапустите сервис.')))));
  try{const settings=await api('/settings');const slot=document.getElementById('totp-settings');if(slot)setChildren(slot,settingRow('Двухфакторная защита',settings.totp_enabled?'Включена':'Не включена'),el('div',{class:'mt-18'},button(settings.totp_enabled?'Отключить 2FA':'Настроить 2FA',()=>settings.totp_enabled?disableTotp():setupTotp(),settings.totp_enabled?'ghost':'primary','shield')));}catch(e){const slot=document.getElementById('totp-settings');if(slot)setChildren(slot,el('p',{class:'form-error'},errorText(e)));}
}
function settingRow(label,value){return el('div',{class:'settings-row'},el('span',{},label),el('strong',{},value));}

function closeModal(){S.modalRequest++;S.modal=null;S.taskDetail=null;if(modal.open)modal.close();setChildren(modal,);}
modal.addEventListener('cancel',e=>{e.preventDefault();closeModal();});
modal.addEventListener('click',e=>{if(e.target===modal){const r=modal.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeModal();}});
function showModal(title,body,footer,opts={}){S.modalRequest++;const scroll=modal.scrollTop;modal.className=opts.wide?'wide':'';setChildren(modal,el('div',{class:'modal-header'},el('div',{},opts.eyebrow?el('div',{class:'eyebrow'},opts.eyebrow):null,el('h2',{id:'modal-title'},title)),iconButton('Закрыть окно','close',closeModal)),body,footer);if(!modal.open)modal.showModal();if(opts.preserveScroll)modal.scrollTop=scroll;}
function input(name,value='',attrs={}){return el('input',{class:'input',name,value,...attrs});}
function textarea(name,value='',attrs={}){return el('textarea',{class:'input',name,...attrs},value);}
function select(name,options,value,attrs={}){const s=el('select',{class:'input',name,...attrs},options.map(o=>el('option',{value:typeof o==='string'?o:o[0],selected:(typeof o==='string'?o:o[0])===value},typeof o==='string'?o:o[1])));if(value!==undefined)s.value=value;return s;}
let fieldSequence=0;
function field(label,control,help){
  const id=`field-${++fieldSequence}`;const single=control.matches('input,select,textarea');
  const caption=el('span',{class:'field-label',id:`${id}-label`},label);
  if(single){if(!control.id)control.id=id;if(help)control.setAttribute('aria-describedby',`${id}-help`);}
  return el(single?'label':'div',{class:'field',for:single?control.id:null,role:single?null:'group','aria-labelledby':single?null:`${id}-label`},caption,control,help?el('span',{class:'field-help',id:`${id}-help`},help):null);
}
function check(name,label,help,checked=false){return el('label',{class:'check-row'},el('input',{type:'checkbox',name,checked}),el('span',{},el('strong',{},label),help?el('small',{},help):null));}
function formModal(title,fields,submit,opts={}){
  const context=S.modal={type:'form'};const error=el('div',{class:'form-error',role:'alert'});const form=el('form',{},el('div',{class:'modal-body form-grid'},fields,error));
  const submitButton=el('button',{type:'submit',class:'btn primary'},opts.submitLabel||'Сохранить');
  form.append(el('div',{class:'modal-footer'},button('Отмена',closeModal,'ghost'),submitButton));
  form.addEventListener('submit',async e=>{
    e.preventDefault();if(submitButton.disabled||!form.reportValidity())return;
    error.textContent='';submitButton.disabled=true;form.setAttribute('aria-busy','true');
    try{const afterSave=await submit(new FormData(form),form);await finishModalAction(context,afterSave,opts.keepOpen===true);}
    catch(err){if(S.modal===context)error.textContent=errorText(err);else toast(errorText(err),true);}
    finally{submitButton.disabled=false;form.removeAttribute('aria-busy');}
  });
  showModal(title,form,null,opts);return form;
}
async function finishModalAction(context,afterSave,keepOpen=false){
  const stillOpen=S.modal===context;
  if(stillOpen&&!keepOpen)closeModal();
  const request=S.modalRequest;
  await refresh(true);
  if(stillOpen&&S.modalRequest===request&&typeof afterSave==='function')await afterSave();
}
function enabledProviders(){return S.data.providers.filter(p=>p.enabled!==false).map(p=>[p.id,`${p.name} · ${p.model||providerKinds[p.kind]}`]);}
function taskForm(task){
  const editing=Boolean(task);
  const started=Boolean(task?.steps?.some(s=>s.attempts>0));
  const providerOptions=[['','Выберите модель оркестратора'],...enabledProviders()];
  const fields=[
    field('Название задачи',input('title',task?.title||'',{required:true,maxlength:200,placeholder:'Например: подготовить контент-план на неделю'})),
    field('Что нужно сделать',textarea('brief',task?.brief||'',{required:true,rows:5,maxlength:30000,disabled:started,placeholder:'Опишите цель, исходные данные, ограничения и формат результата.'}),started?'После начала выполнения исходное задание зафиксировано. Для другой цели создайте новую задачу.':null),
    field('Модель оркестратора',select('planner_provider_id',providerOptions,task?.planner_provider_id||(providerOptions.length===2?providerOptions[1][0]:''),{required:true,disabled:started}),'Эта модель строит план и назначает агентов.'),
    el('div',{class:'form-columns'},
      field('Максимум этапов',input('max_steps',task?.max_steps??8,{type:'number',min:Math.max(1,task?.steps?.length||1),max:16,required:true,disabled:started})),
      field('Лимит выходных токенов',input('max_output_tokens',task?.max_output_tokens??24000,{type:'number',min:1024,max:200000,required:true})))
  ];
  if(!editing)fields.unshift(field('Проект',select('project_id',[['','Без проекта'],...S.data.projects.filter(p=>p.enabled).map(p=>[p.id,p.name])],S.projectId||'')));
  if(!editing)fields.push(check('auto_run','Запустить сразу после планирования','Иначе вы сможете проверить план, инструкции и модели перед запуском.',false));
  formModal(editing?'Изменить задачу':'Новая задача',fields,async data=>{
    const payload={title:data.get('title').trim(),max_output_tokens:Number(data.get('max_output_tokens'))};
    if(!started)Object.assign(payload,{brief:data.get('brief').trim(),planner_provider_id:data.get('planner_provider_id'),max_steps:Number(data.get('max_steps'))});
    if(editing){await api(`/tasks/${task.id}`,'PATCH',payload);toast('Задача сохранена');return ()=>openTask(task.id);}
    else{
      payload.project_id=data.get('project_id')||'';
      payload.auto_run=data.has('auto_run');
      const created=await api('/tasks','POST',payload);
      toast('Задача создана');
      try{await api(`/tasks/${created.id}/plan`,'POST',{});}catch(err){toast(`Задача сохранена, но планирование не запущено: ${errorText(err)}`,true);}
      return ()=>openTask(created.id);
    }
  },{submitLabel:editing?'Сохранить':'Создать и спланировать',eyebrow:editing?`Задача #${shortId(task.id)}`:'От цели к готовому результату'});
}
function ideaForm(){
  const controllers=S.data.connectors.filter(c=>c.kind==='telegram'&&c.enabled!==false&&c.config?.controller_enabled!==false&&c.config?.planner_provider_id);
  if(!controllers.length){S.modal={type:'info'};showModal('Настройте приём идей',el('div',{class:'modal-body form-grid'},notice('В разделе «Интеграции» подключите Telegram-бота, включите управление и выберите модель оркестратора. Один процесс согласования будет доступен и здесь, и в Telegram.')),el('div',{class:'modal-footer'},button('Настроить Telegram',()=>{closeModal();navigate('integrations');connectorForm();},'primary')));return;}
  formModal('Новая идея',[field('Что хотите сделать',textarea('text','',{required:true,rows:7,maxlength:16000,placeholder:'Опишите идею своими словами. Агент предложит решение и дождётся вашего одобрения.'})),field('Настройки приёма идей',select('connector_id',controllers.map(c=>[c.id,c.name]),controllers[0].id,{required:true})),notice('Сначала модель подготовит предложение. Выполнение начнётся только после «Одобрить и выполнить». Публикации согласовываются отдельно.')],async data=>{const task=await api('/ideas','POST',{text:data.get('text').trim(),connector_id:data.get('connector_id')});toast('Идея передана на обработку');return ()=>openTask(task.id);},{submitLabel:'Отправить идею'});
}
function intakePanel(task){
  const intake=task.intake;const labels={queued:'Идея в очереди',analyzing:'Агент обрабатывает идею',review:'Предложение на согласовании',approved:'Предложение одобрено',rejected:'Предложение отклонено',failed:'Не удалось подготовить предложение'};
  const panel=el('section',{class:'detail-section intake-panel'},el('h3',{},labels[intake.state]||intake.state),el('p',{class:'muted'},`Версия ${intake.revision||1}`));
  if(intake.proposal)panel.append(el('pre',{class:'output'},typeof intake.proposal==='string'?intake.proposal:JSON.stringify(intake.proposal,null,2)));
  if(['queued','analyzing'].includes(intake.state))panel.append(el('p',{},'Ответ появится здесь автоматически. До вашего одобрения агенты не начнут выполнение.'));
  if(['review','failed'].includes(intake.state)&&task.status!=='cancelled'){
    const actions=el('div',{class:'toolbar-actions'});
    if(intake.state==='review')actions.append(button('Одобрить и выполнить',()=>intakeAction(task,'approve'),'primary','check'));
    actions.append(button(intake.state==='failed'?'Исправить и повторить':'Внести правки',()=>intakeAction(task,'revise'),'','edit'),button('Отклонить',()=>intakeAction(task,'reject'),'ghost','stop'));panel.append(actions);
  }
  return panel;
}
function intakeBudgetForm(task){
  formModal('Лимит токенов идеи',[el('p',{},`Учтено: ${count(task.tokens_used)} токенов. Текущий лимит: ${count(task.max_output_tokens)}.`),field('Новый лимит выходных токенов',input('max_output_tokens',task.max_output_tokens??24000,{type:'number',min:1024,max:200000,required:true})),notice('Увеличение лимита разрешает дополнительные запросы к модели и может увеличить расходы. После сохранения отправьте правки через «Исправить и повторить», если обработка завершилась ошибкой.')],async data=>{await api(`/tasks/${task.id}`,'PATCH',{max_output_tokens:Number(data.get('max_output_tokens'))});toast('Лимит сохранён');return ()=>openTask(task.id);},{submitLabel:'Сохранить лимит'});
}
function intakeAction(task,action){
  const revision=task.intake.revision;
  if(action==='revise'){formModal('Правки к предложению',[field('Что изменить',textarea('feedback','',{required:true,rows:5,maxlength:4000,placeholder:'Уточните цель, ограничения или желаемый результат.'}))],async data=>{await api(`/tasks/${task.id}/intake/revise`,'POST',{revision,feedback:data.get('feedback').trim()});toast('Правки отправлены');return ()=>openTask(task.id);},{submitLabel:'Отправить правки'});return;}
  confirmModal(action==='approve'?'Одобрить и выполнить?':'Отклонить предложение?',action==='approve'?'Оркестратор создаст план, назначит агентов и запустит выполнение. Запросы к подключённым API могут быть платными. Публикация контента потребует отдельного подтверждения.':'Агенты не будут выполнять эту идею.',async()=>{await api(`/tasks/${task.id}/intake/${action}`,'POST',{revision});return ()=>openTask(task.id);},action==='approve'?'Одобрить и выполнить':'Отклонить',action==='reject');
}
async function ensureTaskResources(task){
  const steps=task?.steps||[];
  const resourcesMissing=steps.some(step=>{
    const agent=lookup('agents',step.agent_id);
    return !agent || !lookup('providers',agent.provider_id);
  });
  if(!resourcesMissing)return;
  // A plan may finish between the state snapshot and the task-detail response.
  // Fetch after that task response so newly persisted agents are selectable.
  const latest=await api('/state');
  S.data.agents=Array.isArray(latest.agents)?latest.agents:[];
  S.data.providers=Array.isArray(latest.providers)?latest.providers:[];
  if(steps.some(step=>!lookup('agents',step.agent_id))){
    throw new Error('В плане есть недоступный агент. Обновите задачу или создайте новый план.');
  }
}
async function openTask(id,tab='pipeline'){
  const request=++S.modalRequest;
  try{
    const detail=await api(`/tasks/${id}`);
    await ensureTaskResources(detail.task);
    if(request!==S.modalRequest)return;
    S.modal={type:'task',id,tab,editing:false};
    S.taskDetail=detail;
    renderTaskDetail();
  }catch(e){toast(errorText(e),true);}
}
function pendingIntake(task){return Boolean(task.intake&&task.intake.state!=='approved');}
function taskActions(task){if(pendingIntake(task))return ['done','cancelled'].includes(task.status)?[]:['cancel'];const out=[];if(task.status==='backlog')out.push('plan');if(task.status==='ready'){out.push('run');if(!(task.steps||[]).some(s=>s.attempts>0))out.push('plan');}if(task.status==='running')out.push('pause');if(task.status==='paused')out.push('resume');if(task.status==='failed')out.push('retry');if(!['done','cancelled'].includes(task.status))out.push('cancel');return out;}
function renderTaskDetail(){
  if(!S.modal||S.modal.type!=='task'||!S.taskDetail)return;const task=S.taskDetail.task;if(!task)return;const tab=S.modal.tab;const steps=task.steps||[];
  const meta=el('div',{class:'detail-meta'},badge(task.status),el('span',{},`${steps.filter(s=>s.status==='done').length}/${steps.length} этапов`),el('span',{},`${count(task.tokens_used)} токенов использовано`),button('Скопировать ID',()=>copyText(task.id),'small ghost','copy'));
  if(task.intake)meta.append(el('span',{},task.source==='telegram'?'Из Telegram':'Идея из веб-интерфейса'));
  const title=el('div',{},el('h2',{id:'modal-title'},task.title),meta);
  const header=el('div',{class:'modal-header'},el('div',{},el('div',{class:'eyebrow'},`ЗАДАЧА #${shortId(task.id)} · ${date(task.created_at,true)}`),title),iconButton('Закрыть задачу','close',closeModal));
  const actionbar=el('div',{class:'detail-actionbar'},taskActions(task).map(action=>button(action==='plan'&&task.status==='ready'?'Перепланировать':actionNames[action],()=>taskAction(task,action),action==='cancel'?'ghost small':action==='pause'||(action==='plan'&&task.status==='ready')?'small':'small primary',{plan:'graph',run:'play',pause:'pause',resume:'play',retry:'refresh',cancel:'stop'}[action])));
  if(pendingIntake(task)&&task.status==='review')actionbar.append(button('Лимит токенов',()=>intakeBudgetForm(task),'small ghost','settings'));
  if(!pendingIntake(task)&&['backlog','ready','paused','failed'].includes(task.status))actionbar.append(button('Описание',()=>taskForm(task),'small ghost','edit'));
  if(!pendingIntake(task)&&task.status==='ready'&&steps.every(s=>s.status==='pending'))actionbar.append(button('Изменить план',()=>graphEditor(task),'small ghost','graph'));
  if(task.result)actionbar.append(button('Создать публикацию',()=>publicationForm(null,task),'small','send'));
  actionbar.append(el('a',{class:'btn small ghost',href:`/api/tasks/${encodeURIComponent(task.id)}/export`,download:''},icon('file',16),'Скачать отчёт'));
  const tabs=el('div',{class:'detail-tabs',role:'tablist'},[['pipeline','План и агенты'],['brief','Описание'],['result','Результат'],['events','Журнал']].map(([id,label])=>el('button',{class:`detail-tab${tab===id?' active':''}`,role:'tab','aria-selected':tab===id,onclick:()=>{S.modal.tab=id;renderTaskDetail();}},label)));
  const content=el('div',{class:'detail-content',role:'tabpanel'});
  if(task.intake&&tab==='pipeline')content.append(intakePanel(task));
  if(task.error)content.append(el('div',{class:'form-error mb-20'},task.error));
  if(tab==='brief'){content.append(el('div',{class:'detail-section'},el('h3',{},'Исходная задача'),el('p',{class:'detail-brief'},task.brief)),el('div',{class:'form-columns'},settingRow('Модель оркестратора',lookup('providers',task.planner_provider_id)?.name||'Не назначена'),settingRow('Лимит выходных токенов',count(task.max_output_tokens))),settingRow('Автозапуск',task.auto_run?'Включён':'Выключен'));
  }else if(tab==='result'){
    if(task.result)content.append(el('div',{class:'copy-row'},el('span',{class:'eyebrow'},'Результат выполнения'),button('Копировать',()=>copyText(task.result),'small','copy')),el('pre',{class:'output'},task.result));else content.append(el('p',{class:'blank-message'},'Итог появится после выполнения этапов. Промежуточные ответы доступны в плане.'));
  }else if(tab==='events'){content.append((S.taskDetail.events||[]).length?eventList(S.taskDetail.events.slice().sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at)))):el('p',{class:'blank-message'},'У этой задачи пока нет событий.'));
  }else if(pendingIntake(task)){/* The intake panel is the only action surface before approval. */
  }else if(!steps.length){content.append(emptyState('graph',task.status==='planning'?'Оркестратор составляет план':'План ещё не создан',task.status==='planning'?'Модель определяет этапы, зависимости и инструкции для агентов. Статус обновится автоматически.':'Запустите планирование — оркестратор разделит цель на этапы и создаст нужных агентов.'));
  }else{content.append(el('p',{class:'pipeline-help'},'Связи показывают, какие ответы получает каждый агент. Независимые этапы могут выполняться параллельно.'),el('div',{class:'pipeline'},steps.map((step,i)=>{const agent=lookup('agents',step.agent_id);const dependencies=(step.depends_on||[]).map(id=>steps.find(s=>s.id===id)?.name||id);return el('article',{class:'pipeline-step'},el('div',{class:'step-head'},el('span',{class:`step-number${step.status==='done'?' step-check':''}`},step.status==='done'?icon('check',15):String(i+1).padStart(2,'0')),el('div',{class:'step-title'},el('strong',{},step.name),el('small',{},`${agent?.name||'Агент не найден'} · ${agent?.model||lookup('providers',agent?.provider_id)?.model||'Модель не указана'}`)),badge(step.status)),el('div',{class:'step-deps'},dependencies.length?`Зависит от: ${dependencies.join(' • ')}`:'Вход: исходная задача · можно выполнять независимо'),step.input?el('div',{class:'step-deps'},`Задание: ${step.input}`):null,step.error?el('div',{class:'step-output'},el('p',{class:'form-error'},step.error)):null,step.output?el('details',{class:'step-output'},el('summary',{},`Ответ агента · ${count(step.tokens_used)} токенов`),el('pre',{class:'output compact'},step.output)):null);})));
  }
  const scroll=modal.scrollTop;modal.className='wide';setChildren(modal,header,actionbar,tabs,content);if(!modal.open)modal.showModal();modal.scrollTop=scroll;
}
async function taskAction(task,action){
  if(action==='cancel'){confirmModal('Отменить задачу?',`Задача «${task.title}» будет остановлена. Уже выполняющийся запрос может завершиться, но следующие этапы не запустятся.`,async()=>{await api(`/tasks/${task.id}/cancel`,'POST',{});toast('Отмена задачи запрошена');return ()=>openTask(task.id);},'Отменить задачу',true);return;}
  try{await api(`/tasks/${task.id}/${action}`,'POST',{});toast(action==='pause'?'Пауза запрошена. Текущие запросы могут завершиться.':'Действие принято');await refresh(true);}catch(e){toast(errorText(e),true);}
}
function graphEditor(task){
  const steps=structuredClone(task.steps||[]);const editor=el('div',{class:'graph-editor'});const agentOptions=S.data.agents.filter(a=>a.enabled!==false).map(a=>[a.id,a.name]);
  function renderEditor(){setChildren(editor,steps.map((step,i)=>{
    const deps=select(`deps_${i}`,steps.filter(s=>s.id!==step.id).map(s=>[s.id,s.name]),undefined,{multiple:true,'aria-label':'Предыдущие этапы'});for(const option of deps.options)option.selected=(step.depends_on||[]).includes(option.value);deps.addEventListener('change',()=>step.depends_on=Array.from(deps.selectedOptions).map(o=>o.value));
    const name=input(`name_${i}`,step.name,{required:true,maxlength:200,oninput:e=>step.name=e.target.value});const agent=select(`agent_${i}`,agentOptions,step.agent_id,{required:true,onchange:e=>step.agent_id=e.target.value});
    return el('div',{class:'graph-edit-step'},el('div',{class:'graph-edit-head'},`ЭТАП ${String(i+1).padStart(2,'0')}`,iconButton('Удалить этап','trash',()=>{steps.splice(i,1);steps.forEach(s=>s.depends_on=(s.depends_on||[]).filter(id=>id!==step.id));renderEditor();})),field('Название этапа',name),field('Агент',agent),field('Дополнительное задание',textarea(`input_${i}`,step.input||'',{rows:2,oninput:e=>step.input=e.target.value})),field('Зависит от этапов',deps,'Выберите несколько с Ctrl / Cmd. Пустой выбор — независимый этап. Циклические связи запрещены.'));
  }));}
  renderEditor();const add=button('Добавить этап',()=>{if(!agentOptions.length){toast('Сначала создайте хотя бы одного активного агента',true);return;}steps.push({id:crypto.randomUUID().replaceAll('-',''),name:`Этап ${steps.length+1}`,agent_id:agentOptions[0][0],depends_on:[],status:'pending',input:'',output:'',error:'',attempts:0,tokens_used:0});renderEditor();},'ghost','plus');
  formModal('Редактор плана',[notice('План можно менять до первого запуска. Выберите агентов и укажите зависимости между этапами.'),editor,add],async()=>{if(!steps.length)throw new Error('Добавьте хотя бы один этап.');await api(`/tasks/${task.id}`,'PATCH',{steps});toast('План сохранён');return ()=>openTask(task.id);},{wide:true,submitLabel:'Сохранить план',eyebrow:`Задача #${shortId(task.id)}`});
}
function agentForm(agent){
  const providers=[['','Выберите провайдера'],...enabledProviders()];
  formModal(agent?'Настроить агента':'Новый агент',[...agentExtraFields(agent),el('div',{class:'form-columns'},field('Имя',input('name',agent?.name||'',{required:true,maxlength:100,placeholder:'Например: Редактор'})),field('Роль',input('role',agent?.role||'',{required:true,maxlength:200,placeholder:'Подготовка и редактура текста'}))),field('Системные инструкции',textarea('instructions',agent?.instructions||'',{required:true,rows:6,maxlength:30000,placeholder:'Опишите специализацию, порядок работы, ограничения и формат ответа.'})),field('Провайдер',select('provider_id',providers,agent?.provider_id||'',{required:true})),field('Модель агента',input('model',agent?.model||'',{maxlength:150,placeholder:'Оставьте пустым для модели провайдера'}),'Можно назначить отдельную модель в рамках выбранного провайдера.'),el('div',{class:'form-columns'},field('Температура',input('temperature',agent?.temperature??.3,{type:'number',step:.1,min:0,max:2,required:true}),'Допустимый диапазон зависит от модели.'),field('Максимум токенов ответа',input('max_tokens',agent?.max_tokens??2048,{type:'number',min:128,max:32000,required:true}))),check('enabled','Агент включён','Доступен для новых задач и назначения в план.',agent?.enabled!==false)],async data=>{const payload={...agentExtraPayload(data),name:data.get('name').trim(),role:data.get('role').trim(),instructions:data.get('instructions'),provider_id:data.get('provider_id'),model:data.get('model').trim(),temperature:Number(data.get('temperature')),max_tokens:Number(data.get('max_tokens')),enabled:data.has('enabled')};await api(agent?`/agents/${agent.id}`:'/agents',agent?'PATCH':'POST',payload);toast(agent?'Агент обновлён':'Агент создан');},{eyebrow:'Команда / агент'});
}
function providerForm(provider){
  const kinds=Object.entries(providerKinds).filter(([k])=>k!=='mock'||S.data.settings.demo_mode||S.session.demo_mode||provider?.kind==='mock');
  const kind=select('kind',kinds,provider?.kind||'openai');const dynamic=el('div',{class:'form-grid'});const values={base_url:provider?.base_url||'',model:provider?.model||'',api_key:''};
  function update(){const isMock=kind.value==='mock';const compat=kind.value==='openai_compatible';setChildren(dynamic,!isMock?field('API-ключ',input('api_key','',{type:'password',autocomplete:'new-password',required:!provider||provider.kind!==kind.value,maxlength:4096,placeholder:provider?'Оставьте пустым, чтобы сохранить текущий ключ':'Вставьте API-ключ'}),'Ключ сохраняется в зашифрованном виде и не включается в ответы агентов.'):notice('Демо-модель возвращает тестовый результат. Это не подключение к реальному ИИ.',true),field('Идентификатор модели',input('model',values.model,{required:!isMock,maxlength:150,placeholder:isMock?'demo':'Точное имя модели из кабинета провайдера',oninput:e=>values.model=e.target.value})),compat?field('Базовый адрес API',input('base_url',values.base_url,{type:'url',required:true,placeholder:'https://api.example.com/v1',oninput:e=>values.base_url=e.target.value}),'HTTPS-адрес совместимого API. Внутренние адреса и localhost не поддерживаются.'):null);}
  kind.addEventListener('change',update);update();
  formModal(provider?'Настройки провайдера':'Подключить модель',[field('Название подключения',input('name',provider?.name||'',{required:true,maxlength:100,placeholder:'Например: Основная модель'})),field('Провайдер',kind),dynamic,check('enabled','Подключение включено','Модель доступна оркестратору и агентам.',provider?.enabled!==false)],async data=>{const payload={name:data.get('name').trim(),kind:data.get('kind'),model:String(data.get('model')||(data.get('kind')==='mock'?'demo':'')).trim(),base_url:data.get('kind')==='openai_compatible'?String(data.get('base_url')||'').trim():({openai:'https://api.openai.com/v1',anthropic:'https://api.anthropic.com/v1',gemini:'https://generativelanguage.googleapis.com/v1beta'}[data.get('kind')]||''),enabled:data.has('enabled')};const key=String(data.get('api_key')||'').trim();if(key)payload.api_key=key;await api(provider?`/providers/${provider.id}`:'/providers',provider?'PATCH':'POST',payload);toast(provider?'Провайдер обновлён':'Провайдер добавлен');},{eyebrow:'Модели / подключение',submitLabel:provider?'Сохранить':'Подключить'});
}
async function testResource(kind,resource,btn){
  if(btn)btn.disabled=true;
  try{
    const result=await api(`/${kind}/${resource.id}/test`,'POST',{});
    S.modal={type:'info'};
    const message=typeof result==='string'?result:result.message||'Проверка выполнена. Результат приведён ниже.';
    const body=el('div',{class:'modal-body form-grid'},el('p',{class:'eyebrow'},resource.name),el('p',{class:'detail-brief'},message));
    if(typeof result==='object')body.append(el('details',{},el('summary',{class:'subtle-link'},'Технические детали'),el('pre',{class:'output compact'},JSON.stringify(result,null,2))));
    showModal('Результат проверки',body,el('div',{class:'modal-footer'},button('Понятно',closeModal,'primary')));
  }catch(e){toast(errorText(e),true);}
  finally{if(btn)btn.disabled=false;}
}
function deleteResource(kind,resource){const labels={agents:'агента',providers:'провайдера',connectors:'интеграцию',publications:'черновик'};confirmModal(`Удалить ${labels[kind]}?`,`«${resource.name}» будет удалён. Если объект используется другими задачами или настройками, сервер может запретить удаление.`,async()=>{await api(`/${kind}/${resource.id}`,'DELETE');toast('Удалено');},'Удалить',true);}
function confirmModal(title,message,onConfirm,label='Подтвердить',danger=false){
  const context=S.modal={type:'confirm'};const error=el('p',{class:'form-error',role:'alert'});
  const b=button(label,async()=>{
    if(b.disabled)return;b.disabled=true;error.textContent='';
    try{const afterSave=await onConfirm();await finishModalAction(context,afterSave);}
    catch(e){if(S.modal===context)error.textContent=errorText(e);else toast(errorText(e),true);}
    finally{b.disabled=false;}
  },danger?'danger':'primary');
  showModal(title,el('div',{class:'modal-body form-grid'},el('p',{class:'delete-warning'},message),error),el('div',{class:'modal-footer'},button('Назад',closeModal,'ghost'),b));
}

const connectorFields={
  telegram:{help:'Создайте бота через @BotFather. Узнайте свой числовой Telegram user ID и добавьте его в разрешённый список. Бот работает через long polling; внешний webhook для него не нужен.',config:[['allowed_user_ids','Разрешённые Telegram user ID','text','123456789, 987654321',true],['chat_id','ID чата (необязательно)','text','-1001234567890 или @channel',false]],secrets:[['bot_token','Токен бота',true]]},
  vk:{help:'Текстовые публикации на стене. Для сообщества owner_id указывается со знаком минус. У токена должны быть права на размещение публикаций. Прикрепление медиа пока не поддерживается.',config:[['owner_id','Владелец стены · owner_id','text','-123456789',true],['api_version','Версия API','text','5.199',true]],secrets:[['access_token','Токен доступа VK',true]]},
  instagram:{help:'Нужен профессиональный аккаунт и Instagram API access token с правами публикации. Медиа должно быть доступно по публичной HTTPS-ссылке. Версию API проверьте в кабинете Meta.',config:[['account_id','Instagram account ID','text','Числовой ID профессионального аккаунта',true],['api_version','Версия Graph API','text','Например: v23.0',true],['media_type','Тип публикации','select',[['IMAGE','Изображение'],['REELS','Reels']],true]],secrets:[['access_token','Токен доступа Instagram',true]]},
  youtube:{help:'Нужен OAuth-клиент Google, включённый YouTube Data API и refresh token с доступом к загрузке видео. По умолчанию видео загружаются приватно.',config:[['client_id','Google OAuth Client ID','text','…apps.googleusercontent.com',true],['privacy_status','Видимость загруженного видео','select',[['private','Приватное'],['unlisted','По ссылке'],['public','Публичное']],true]],secrets:[['client_secret','Google Client Secret',true],['refresh_token','OAuth Refresh Token',true]]},
  webhook:{help:'При публикации система отправит JSON на ваш HTTPS endpoint с авторизацией Bearer. Локальные и приватные адреса запрещены. Проверка соединения проверяет адрес и DNS, не запускает webhook.',config:[['url','HTTPS URL','url','https://automation.example.com/hooks/content',true]],secrets:[['bearer_token','Секрет Bearer',true]]}
};
function connectorForm(connector){
  const kind=select('kind',Object.entries(connectorKinds),connector?.kind||'telegram',{disabled:Boolean(connector)});const dynamic=el('div',{class:'form-grid'});let currentKind=kind.value;const drafts={};
  function capture(){const values={};for(const control of dynamic.querySelectorAll('[name]'))values[control.name]=control.type==='checkbox'?control.checked:control.value;drafts[currentKind]=values;}
  function draw(){currentKind=kind.value;const definition=connectorFields[currentKind];const cfg=connector?.kind===currentKind?connector.config||{}:{};const saved=drafts[currentKind]||{};const value=(key,fallback='')=>saved[`config_${key}`]??cfg[key]??fallback;
    const controls=definition.config.map(([key,label,type,placeholder,required])=>{let v=value(key,key==='api_version'&&currentKind==='vk'?'5.199':key==='media_type'?'IMAGE':key==='privacy_status'?'private':'');if(Array.isArray(v))v=v.join(', ');return field(key==='chat_id'?'ID канала для публикаций (необязательно)':label,type==='select'?select(`config_${key}`,placeholder,v,{required}):input(`config_${key}`,v,{type,placeholder,required:currentKind==='telegram'?false:required}));});
    if(currentKind==='telegram'){
      const providers=[['','Выберите модель'],...enabledProviders()];
      controls.push(field('Режим бота',select('config_mode',[['ideas','Приём идей и согласование плана'],['chat','Диалог с выбранным агентом']],value('mode','ideas'))),field('Проект бота',select('config_project_id',[['','Без проекта'],...S.data.projects.filter(p=>p.enabled).map(p=>[p.id,p.name])],value('project_id'))),field('Агент для диалога',select('config_agent_id',[['','Выберите агента'],...S.data.agents.filter(a=>a.enabled).map(a=>[a.id,a.name])],value('agent_id'))),check('config_controller_enabled','Управление через личный чат','Отключите, если бот нужен только для публикаций в канал.',value('controller_enabled',true)),field('Модель оркестратора',select('config_planner_provider_id',providers,value('planner_provider_id'))),field('Модель первого агента',select('config_intake_provider_id',[['','Использовать модель оркестратора'],...enabledProviders()],value('intake_provider_id'))),field('Инструкции первого агента',textarea('config_intake_instructions',value('intake_instructions'),{rows:4,maxlength:8000,placeholder:'Как обрабатывать идеи, что учитывать и в каком формате предлагать решения.'})),check('config_notifications_enabled','Уведомления о ходе выполнения','Бот сообщит о результатах и запросит необходимые решения.',value('notifications_enabled',true)),check('config_voice_enabled','Принимать голосовые сообщения','Аудио передаётся в выбранный OpenAI API для расшифровки. Возможна оплата по тарифам провайдера.',value('voice_enabled',false)),field('OpenAI для расшифровки',select('config_transcription_provider_id',[['','Выберите OpenAI'],...S.data.providers.filter(p=>p.enabled!==false&&p.kind==='openai').map(p=>[p.id,p.name])],value('transcription_provider_id'))),field('Модель расшифровки',select('config_transcription_model',['gpt-4o-mini-transcribe','gpt-4o-transcribe','whisper-1','gpt-4o-mini-transcribe-2025-12-15'].map(m=>[m,m]),value('transcription_model','gpt-4o-mini-transcribe'))),el('div',{class:'form-columns'},field('Максимум этапов',input('config_max_steps',value('max_steps',8),{type:'number',min:1,max:16,required:true})),field('Лимит выходных токенов',input('config_max_output_tokens',value('max_output_tokens',24000),{type:'number',min:1024,max:200000,required:true}))));
    }
    setChildren(dynamic,el('p',{class:'content-type-note'},definition.help),controls,...definition.secrets.map(([key,label,required])=>field(label,input(`secret_${key}`,saved[`secret_${key}`]||'',{type:'password',autocomplete:'new-password',required:required&&(!connector||connector.kind!==currentKind),placeholder:connector&&connector.kind===currentKind?'Пусто — сохранить текущий секрет':'Вставьте секрет'}))));
  }
  kind.addEventListener('change',()=>{capture();draw();});draw();
  formModal(connector?'Настроить интеграцию':'Подключить канал',[field('Название',input('name',connector?.name||'',{required:true,maxlength:100,placeholder:'Например: Мой Telegram-бот'})),field('Площадка',kind),dynamic,check('enabled','Интеграция включена','Telegram-бот начнёт принимать разрешённые команды после сохранения.',connector?.enabled!==false)],async data=>{
    const config={};const secrets={};const telegram=currentKind==='telegram';const controller=telegram&&data.has('config_controller_enabled');
    for(const[key]of connectorFields[currentKind].config){let value=data.get(`config_${key}`);if(key==='allowed_user_ids'){const raw=String(value||'').split(/[,;\s]+/).filter(Boolean);if((controller&&!raw.length)||raw.length>10||raw.some(id=>!/^\d+$/.test(id)||!Number.isSafeInteger(Number(id))||Number(id)<=0))throw new Error('Для управления укажите от 1 до 10 положительных Telegram user ID.');value=raw.map(Number);}else if(key==='chat_id'&&value){value=String(value).trim();if(/^-?\d+$/.test(value)&&Number.isSafeInteger(Number(value)))value=Number(value);else if(!/^@[A-Za-z][A-Za-z0-9_]{4,31}$/.test(value))throw new Error('Укажите числовой ID канала или @username.');}else if(key==='owner_id'&&value){if(!/^-?\d+$/.test(value)||!Number.isSafeInteger(Number(value)))throw new Error('ID должен быть целым числом.');value=Number(value);}if(value!==''&&value!==null)config[key]=value;}
    if(telegram){for(const key of ['controller_enabled','voice_enabled','notifications_enabled'])config[key]=data.has(`config_${key}`);for(const key of ['planner_provider_id','intake_provider_id','intake_instructions','transcription_provider_id','transcription_model','project_id','agent_id','mode'])config[key]=String(data.get(`config_${key}`)||'').trim();for(const key of ['max_steps','max_output_tokens'])config[key]=Number(data.get(`config_${key}`));if(controller&&config.mode!=='chat'&&!config.planner_provider_id)throw new Error('Выберите модель оркестратора для приёма идей.');if(config.voice_enabled&&!config.transcription_provider_id)throw new Error('Для голосовых сообщений выберите подключение OpenAI.');}
    for(const[key]of connectorFields[currentKind].secrets){const value=String(data.get(`secret_${key}`)||'').trim();if(value)secrets[key]=value;}const payload={name:data.get('name').trim(),kind:currentKind,config,enabled:data.has('enabled')};if(Object.keys(secrets).length)payload.secrets=secrets;await api(connector?`/connectors/${connector.id}`:'/connectors',connector?'PATCH':'POST',payload);toast(connector?'Интеграция обновлена':'Интеграция добавлена');},{eyebrow:'Интеграции / подключение'});
}
function publicationForm(publication,task){
  const connectors=S.data.connectors.filter(c=>c.enabled!==false&&(c.kind!=='telegram'||c.config?.chat_id));if(!connectors.length){toast('Сначала подключите канал в разделе «Интеграции». Для публикаций в Telegram задайте ID канала.',true);navigate('integrations');closeModal();return;}
  const connector=select('connector_id',connectors.map(c=>[c.id,`${c.name} · ${connectorKinds[c.kind]}`]),publication?.connector_id||connectors[0].id,{required:true});
  const media=input('media_url',publication?.media_url||'',{type:'url',placeholder:'https://… публичная ссылка на файл'});const title=input('title',publication?.title||task?.title||'',{maxlength:200,placeholder:'Заголовок публикации'});const note=el('div',{class:'content-type-note'});
  function update(){const c=lookup('connectors',connector.value);const notes={telegram:'Отправка текста в настроенный chat_id. Для медиа используйте поддерживаемый канал публикации.',vk:'VK: публикуется текст. Медиа не поддерживается этим адаптером.',instagram:'Instagram: публичная HTTPS-ссылка на изображение или видео обязательна. Тип медиа определяется в настройках интеграции.',youtube:'YouTube: укажите название и публичную HTTPS-ссылку на видео. Текст станет описанием. Видимость задаётся в интеграции.',webhook:'Webhook: текст, заголовок и ссылка на медиа будут переданы в JSON на подключённый адрес.'};note.textContent=notes[c?.kind]||'';media.required=['instagram','youtube'].includes(c?.kind);title.required=c?.kind==='youtube';}
  connector.addEventListener('change',update);update();
  formModal(publication?'Изменить черновик':'Новая публикация',[field('Канал',connector),note,field('Заголовок',title),field('Текст / описание',textarea('text',publication?.text||task?.result||'',{rows:7,maxlength:40000})),field('Ссылка на медиа',media,'Файл должен быть доступен площадке без авторизации. Используйте прямую HTTPS-ссылку.'),field('Связанная задача',select('task_id',[['','Без задачи'],...S.data.tasks.map(t=>[t.id,t.title])],publication?.task_id||task?.id||'')),notice('Сохранение создаёт черновик. Для отправки потребуется одобрение и команда «Опубликовать».')],async data=>{const payload={connector_id:data.get('connector_id'),task_id:data.get('task_id')||'',title:data.get('title').trim(),text:data.get('text'),media_url:data.get('media_url').trim()||''};if(!payload.text.trim()&&!payload.media_url)throw new Error('Добавьте текст или ссылку на медиа.');await api(publication?`/publications/${publication.id}`:'/publications',publication?'PATCH':'POST',payload);toast('Черновик сохранён');navigate('content');},{submitLabel:'Сохранить черновик',eyebrow:'Публикации / подготовка'});
}
function confirmPublication(p,action){const connector=lookup('connectors',p.connector_id);const publish=action==='publish';confirmModal(publish?'Опубликовать сейчас?':'Одобрить публикацию?',publish?`Контент будет отправлен в «${connector?.name||'выбранный канал'}». Проверьте текст и медиа перед подтверждением. Статус и результат появятся в карточке публикации.`:`Публикация для «${connector?.name||'выбранного канала'}» будет отмечена как одобренная. Отправка потребует отдельного нажатия «Опубликовать».`,async()=>{await api(`/publications/${p.id}/${action}`,'POST',{}, {'X-Resource-Version':p.updated_at});toast(publish?'Команда публикации принята':'Публикация одобрена');},publish?'Опубликовать':'Одобрить');}
async function setupTotp(){try{const result=await api('/settings/totp/setup','POST',{});formModal('Настроить двухфакторный вход',[el('p',{class:'detail-brief'},'Добавьте аккаунт в приложение-аутентификатор по ключу ниже. Тип: по времени (TOTP). Затем введите полученный шестизначный код.'),el('div',{class:'copy-row'},el('span',{class:'field-label'},'Секрет для аутентификатора'),button('Копировать',()=>copyText(result.secret),'small','copy')),el('div',{class:'secret-block'},result.secret),el('details',{},el('summary',{class:'subtle-link'},'Показать URI для импорта'),el('p',{class:'totp-uri'},result.uri)),field('Код из приложения',input('code','',{required:true,inputmode:'numeric',pattern:'[0-9]{6}',maxlength:6,autocomplete:'one-time-code',placeholder:'000000'}))],async data=>{await api('/settings/totp/enable','POST',{code:data.get('code')});S.session={};closeModal();renderLogin();toast('2FA включена. Войдите заново с новым кодом из приложения.');},{submitLabel:'Включить 2FA',eyebrow:'Настройки / безопасность'});}catch(e){toast(errorText(e),true);}}
function disableTotp(){formModal('Отключить второй фактор',[notice('После отключения для входа будет нужен только пароль владельца.',true),field('Текущий пароль',input('password','',{type:'password',required:true,autocomplete:'current-password'})),field('Код из аутентификатора',input('code','',{required:true,inputmode:'numeric',pattern:'[0-9]{6}',maxlength:6,autocomplete:'one-time-code'}))],async data=>{await api('/settings/totp/disable','POST',{password:data.get('password'),code:data.get('code')});S.session={};closeModal();renderLogin();toast('2FA отключена. Войдите заново с паролем.');},{submitLabel:'Отключить 2FA'});}
async function copyText(value){try{await navigator.clipboard.writeText(String(value));toast('Скопировано');}catch{toast('Буфер обмена недоступен. Выделите текст и скопируйте вручную.',true);}}
function renderLogin(){
  const error=el('div',{class:'form-error',role:'alert'});const submit=el('button',{type:'submit',class:'btn primary'},'Войти в рабочую область',icon('arrow',17));
  const form=el('form',{},field('Имя пользователя',input('username','',{required:true,autocomplete:'username',placeholder:'Имя владельца'})),field('Пароль',input('password','',{type:'password',required:true,autocomplete:'current-password',placeholder:'Введите пароль'})),field('Код 2FA',input('totp','',{inputmode:'numeric',pattern:'[0-9]{6}',maxlength:6,autocomplete:'one-time-code',placeholder:'Если включена двухфакторная защита'}),'Оставьте пустым, если второй фактор ещё не настроен.'),error,submit);
  form.addEventListener('submit',async e=>{e.preventDefault();error.textContent='';submit.disabled=true;try{const data=new FormData(form);await api('/auth/login','POST',{username:data.get('username').trim(),password:data.get('password'),totp:data.get('totp')||''});S.session=await api('/auth/session');S.signature='';await refresh(true);}catch(err){error.textContent=errorText(err);}finally{submit.disabled=false;}});
  setChildren(app,el('div',{class:'login-page'},el('aside',{class:'login-side'},brand(),el('div',{class:'login-copy'},el('div',{class:'eyebrow'},'АГЕНТСКАЯ ОПЕРАЦИОННАЯ СИСТЕМА'),el('h1',{},'Ваша команда.',el('br'),'Ваши правила.'),el('p',{},'Единое место для задач, моделей и агентов. От первого задания до результата — всё под вашим управлением.'),el('div',{class:'login-grid'},[['graph','План','Задачи и зависимости'],['agents','Команда','Роли и инструкции'],['activity','Контроль','Статусы и журнал']].map(([ico,label,sub])=>el('div',{class:'login-node'},icon(ico,23),label,el('small',{},sub))))),el('div',{class:'login-foot'},'СОБСТВЕННЫЙ СЕРВЕР / ЕДИНАЯ РАБОЧАЯ ОБЛАСТЬ')),el('main',{class:'login-main'},el('div',{class:'login-card'},brand('login-mobile-brand'),el('h2',{},'С возвращением'),el('p',{},'Войдите, чтобы продолжить работу с задачами и агентами.'),form,el('div',{class:'login-security'},icon('lock',13),'Доступ только для владельца системы')))));
}
async function init(){S.view=views[location.hash.slice(1)]?location.hash.slice(1):'tasks';try{S.session=await api('/auth/session');if(S.session.authenticated)await refresh(true);else renderLogin();}catch(e){setChildren(app,el('div',{class:'boot'},icon('server',36),el('h2',{},'Сервер недоступен'),el('p',{},errorText(e)),button('Повторить',init,'primary','refresh')));}}
window.addEventListener('hashchange',()=>{const view=location.hash.slice(1);if(views[view]&&view!==S.view){S.view=view;S.search='';renderShell();}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{if(!document.hidden)refresh();},3000);
init();
