'use strict';
const $ = id => document.getElementById(id);
let user = null, csrfToken = '', configuration = {}, conversations = [], conversation = null, tasks = [], timer, panel = '', filePath = '', pendingKey = '', pendingPrompt = '', pendingConversation = '', lastMessages = '', polling = false;
let messageItems=[], messageBefore=null, messagesLoaded=false, messageHistoryBusy=false, chatVersion=0, eventState=null, filesState=null;
let statsTimer=null, statsController=null, statsGeneration=0;
let signup = false;
const bootstrapToken = new URLSearchParams(location.search).get('setup') || '';
if (bootstrapToken) history.replaceState(null, '', '/');
const statusText = {queued:'排队中', running:'执行中', waiting_config:'等待模型配置', succeeded:'已完成', failed:'失败', interrupted:'中断待检查', cancelled:'已停止'};
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, options = {}) {
  const requestUser=user?.id,requestSession=csrfToken;
  const response = await fetch(path, {credentials:'same-origin', ...options, headers:{'Content-Type':'application/json', ...(options.method ? {'X-CSRF-Token':csrfToken} : {}), ...options.headers}});
  const data = await response.json();
  if (!response.ok) {
    if(response.status===401&&user?.id===requestUser&&csrfToken===requestSession)showLogin();
    throw new Error(data.error || '请求失败，请重试');
  }
  return data;
}
function post(path, data) {return api(path, {method:'POST', body:JSON.stringify(data)});}
function toast(message) {$('toast').textContent=message; $('toast').hidden=false; setTimeout(()=>{$('toast').hidden=true;},4500);}
function error(id, message) {$(id).textContent=message; $(id).hidden=!message;}
function authMode() {
  const first = configuration.needs_bootstrap;
  signup = first || signup;
  $('auth-title').textContent=first?'创建你的管理员账户':signup?'创建工作空间':'回到你的工作空间';
  $('auth-submit').textContent=signup?'创建账户':'登录';
  $('confirm-wrap').hidden=!signup;
  $('confirm-password').required=signup;
  $('password').autocomplete=signup?'new-password':'current-password';
  $('auth-switch').hidden=!configuration.signup_enabled || first;
  $('auth-switch').textContent=signup?'已有账户，去登录':'创建账户';
  if (first && !bootstrapToken && !$('bootstrap-input')) {
    const label=document.createElement('label'); label.textContent='初始化口令';
    const input=document.createElement('input'); input.id='bootstrap-input'; input.type='password'; input.autocomplete='off'; input.required=true; input.placeholder='服务器提供的一次性口令';
    label.append(input); $('auth-form').prepend(label);
  }
  if (first) $('auth-note').textContent='先创建管理员，再在设置中接入模型并邀请测试用户。';
}
function showLogin() {clearInterval(timer);stopServerStats();chatVersion++;user=null;csrfToken='';conversation=null;conversations=[];tasks=[];messageItems=[];messagesLoaded=false;messageBefore=null;lastMessages='';pendingKey='';pendingPrompt='';pendingConversation='';panel='';eventState=null;filesState=null;filePath='';$('messages').replaceChildren();$('conversations').replaceChildren();$('panel-content').replaceChildren();$('details').hidden=true;$('prompt').value='';$('app').hidden=true;$('login').hidden=false;updateFilesButton();}
$('auth-switch').onclick=()=>{signup=!signup; authMode(); error('auth-error','');};
$('auth-form').onsubmit=async event=>{
  event.preventDefault(); error('auth-error','');
  if (signup && $('password').value!==$('confirm-password').value) return error('auth-error','两次输入的密码不一致');
  $('auth-submit').disabled=true;
  try {
    const data=await post(signup?'/api/signup':'/api/login',{username:$('username').value,password:$('password').value,bootstrap_token:bootstrapToken || $('bootstrap-input')?.value || ''});
    $('password').value=''; $('confirm-password').value='';
    await enter(data);
  } catch(e) {error('auth-error',e.message);} finally {$('auth-submit').disabled=false;}
};
async function enter(data) {
  user=data.user; csrfToken=data.csrf; configuration=data.config;
  $('login').hidden=true; $('app').hidden=false; $('account-name').textContent=user.username; $('settings-toggle').hidden=!user.is_admin;
  startServerStats();
  updateConfig(); await loadConversations();
  const saved=localStorage.getItem('conversation:'+user.id);
  await selectConversation(conversations.find(c=>c.id===saved) || conversations[0] || null);
  clearInterval(timer); timer=setInterval(poll,2000);
}
function updateConfig() {$('config-banner').hidden=configuration.configured;}
function statsUnavailable(note='暂不可用'){
  for(const name of ['cpu','memory','disk','load']){$('stats-'+name).textContent='—';$('stats-'+name).title='';}
  $('stats-memory-size').textContent='';$('stats-disk-size').textContent='';$('stats-status').textContent=note;$('stats-status').hidden=!note;
}
function stopServerStats(note=''){clearInterval(statsTimer);statsTimer=null;statsGeneration++;statsController?.abort();statsController=null;statsUnavailable(note);}
function startServerStats(){stopServerStats('正在读取服务器状态');if(!user||document.visibilityState==='hidden')return;refreshServerStats();statsTimer=setInterval(refreshServerStats,5000);}
function sizePair(used,total){const unit=total>=1024**3?1024**3:1024**2,label=unit===1024**3?'GiB':'MiB';return (used/unit).toFixed(1)+' / '+(total/unit).toFixed(1)+' '+label;}
function renderServerStats(stats){
  const validUsage=value=>value&&Number.isFinite(value.used_bytes)&&value.used_bytes>=0&&Number.isFinite(value.total_bytes)&&value.total_bytes>0&&Number.isFinite(value.percent)&&value.percent>=0&&value.percent<=100;
  if(!stats||!Number.isFinite(stats.collected_at)){statsUnavailable();return;}
  if(Date.now()/1000-stats.collected_at>Math.max(20,3*(Number.isFinite(stats.sample_seconds)?stats.sample_seconds:5))){statsUnavailable('数据已过期');return;}
  let partial=false;const cores=Number.isFinite(stats.cpu_count)&&stats.cpu_count>=1?stats.cpu_count:null;
  if(stats.cpu_percent===null)$('stats-cpu').textContent='采样中';
  else if(Number.isFinite(stats.cpu_percent)&&stats.cpu_percent>=0&&stats.cpu_percent<=100)$('stats-cpu').textContent=stats.cpu_percent.toFixed(1)+'%';
  else{$('stats-cpu').textContent='—';partial=true;}
  $('stats-cpu').title=cores!==null?cores+' 个 CPU 核心':'核心数暂不可用';
  for(const name of ['memory','disk']){if(validUsage(stats[name])){$('stats-'+name).textContent=stats[name].percent.toFixed(1)+'%';$('stats-'+name+'-size').textContent=sizePair(stats[name].used_bytes,stats[name].total_bytes);}else{$('stats-'+name).textContent='—';$('stats-'+name+'-size').textContent='';partial=true;}}
  if(Array.isArray(stats.load_average)&&Number.isFinite(stats.load_average[0])&&stats.load_average[0]>=0)$('stats-load').textContent=stats.load_average[0].toFixed(2);
  else{$('stats-load').textContent='—';partial=true;}
  $('stats-load').title='最近 1 分钟的平均系统负载 · '+(cores!==null?cores+' 个 CPU 核心':'核心数暂不可用');
  $('stats-status').textContent=partial?'部分指标暂不可用':'';$('stats-status').hidden=!partial;
}
async function refreshServerStats(){
  if(!user||document.visibilityState==='hidden'||statsController)return;const generation=statsGeneration,uid=user.id,controller=new AbortController();statsController=controller;
  const timeout=setTimeout(()=>{if(statsController===controller&&generation===statsGeneration&&user?.id===uid){statsGeneration++;statsController=null;controller.abort();statsUnavailable();}},8000);
  try{const stats=await api('/api/server-stats',{signal:controller.signal});if(generation!==statsGeneration||user?.id!==uid||document.visibilityState==='hidden')return;renderServerStats(stats);}catch(e){if(generation===statsGeneration&&user?.id===uid&&e.name!=='AbortError')statsUnavailable();}finally{clearTimeout(timeout);if(statsController===controller)statsController=null;}
}
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')stopServerStats('已暂停更新');else if(user)startServerStats();});
async function loadConversations() {conversations=await api('/api/conversations'); renderConversations();}
function renderConversations() {
  $('conversations').replaceChildren();
  for(const c of conversations) {const button=document.createElement('button'); button.className='conversation-button'+(conversation?.id===c.id?' selected':''); button.textContent=c.title; button.title=c.title; button.onclick=()=>selectConversation(c); $('conversations').append(button);}
}
async function selectConversation(c) {
  conversation=c;chatVersion++;lastMessages='';messageItems=[];messageBefore=null;messagesLoaded=false;messageHistoryBusy=false;tasks=[];
  filesState=null;filePath='';if(panel==='files'){panel='';$('details').hidden=true;$('panel-content').replaceChildren();}updateFilesButton();
  $('messages').replaceChildren();
  if(c) localStorage.setItem('conversation:'+user.id,c.id); else localStorage.removeItem('conversation:'+user.id);
  $('chat-title').textContent=c?.title || '新对话'; renderConversations();
  if (!c) {$('messages').innerHTML='<div class="empty-chat"><div class="empty-mark">A</div><h2>交给云端，随时回来</h2><p>描述你要完成的任务。文件按对话整理，过程和结果始终保留。</p><div class="suggestions"><button data-prompt="请检查当前工作空间和 Python 环境，创建 hello.py 并运行它。">创建并运行 Python 文件</button><button data-prompt="请整理一个项目计划，保存为当前对话的 plan.md。">把项目计划保存成文件</button></div></div>';return;}
  const loading=document.createElement('p');loading.className='muted';loading.textContent='正在加载对话…';$('messages').append(loading);await refreshConversation();
}
$('new-chat').onclick=()=>{selectConversation(null); $('prompt').focus();};
document.addEventListener('click',event=>{const suggestion=event.target.closest('[data-prompt]'); if(suggestion){$('prompt').value=suggestion.dataset.prompt; $('prompt').focus();}});
function renderMessages(messages) {
  const taskMap=new Map(tasks.map(task=>[task.id,task]));
  const encoded=JSON.stringify([messageBefore,messages,tasks.map(task=>[task.id,task.status,task.cancel_requested,task.error])]);
  if(encoded===lastMessages)return;lastMessages=encoded;
  const view=$('messages'),nearEnd=view.scrollHeight-view.scrollTop-view.clientHeight<150,rect=view.getBoundingClientRect();
  const visible=[...view.querySelectorAll('[data-message-id]')].find(item=>item.getBoundingClientRect().bottom>rect.top);
  const anchor=visible?{id:visible.dataset.messageId,offset:visible.getBoundingClientRect().top-rect.top}:null;
  view.replaceChildren();
  if(messageBefore!==null){const more=document.createElement('button');more.textContent='加载更早消息';more.onclick=loadEarlierMessages;view.append(more);}
  for(const m of messages){const article=document.createElement('article');article.className='message '+m.role;article.dataset.messageId=String(m.id);
    const meta=document.createElement('div');meta.className='message-meta';const who=document.createElement('span');who.textContent=m.role==='user'?user.username:m.role==='assistant'?'Agent':'系统';const when=document.createElement('time');when.textContent=new Date(m.created_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});meta.append(who,when);
    const content=document.createElement('div');content.className='message-content';messageContent(content,m.content);article.append(meta,content);
    if(m.role==='user'&&m.task_id)article.append(messageTask(taskMap.get(m.task_id),m.task_id));
    view.append(article);
  }
  if(nearEnd||messages.length<3)view.scrollTop=view.scrollHeight;
  else if(anchor){const current=[...view.querySelectorAll('[data-message-id]')].find(item=>item.dataset.messageId===anchor.id);if(current)view.scrollTop+=current.getBoundingClientRect().top-view.getBoundingClientRect().top-anchor.offset;}
}
function messageContent(container,value){
  const text=String(value??''),base=configuration.site_base_url||'';
  let origin;try{origin=new URL(base).origin;}catch{container.textContent=text;return;}
  const pattern=/https?:\/\/[^\s<>"'`，。；）\])}]+/g;let offset=0,matched=false;
  for(const match of text.matchAll(pattern)){
    let url;try{url=new URL(match[0]);}catch{continue;}
    const shortPath=/^\/([a-z0-9]{4,10})\/(?:[^\s]*)$/.exec(url.pathname);
    const legacyPath=/^\/sites\/[a-f0-9]{32}\/(?:[^\s]*)$/.test(url.pathname);
    const reserved=['api','sites','static','assets','private','admin','settings','workspace','workspaces','data','backups','runs','sandboxes','health','login','logout','favicon','signup','status','config','published'];
    if(url.origin!==origin||url.username||url.password||(!legacyPath&&(!shortPath||reserved.includes(shortPath[1]))))continue;
    const before=document.createElement('span');before.textContent=text.slice(offset,match.index);container.append(before);
    const link=document.createElement('a');link.href=url.href;link.textContent=match[0];link.target='_blank';link.rel='noopener noreferrer';container.append(link);
    offset=match.index+match[0].length;matched=true;
  }
  if(matched){const after=document.createElement('span');after.textContent=text.slice(offset);container.append(after);}else container.textContent=text;
}
function messageTask(task,taskId){
  const footer=document.createElement('div');footer.className='message-task';const actions=document.createElement('div');actions.className='message-task-actions';
  const badge=document.createElement('span');badge.className='status '+(task?.status||'unknown');badge.textContent=!task?'状态读取中':task.cancel_requested&&task.status==='running'?'正在停止…':statusText[task.status]||task.status;actions.append(badge);
  const log=document.createElement('button');log.textContent='过程';log.onclick=()=>showTask(task||{id:taskId});actions.append(log);
  if(task&&['queued','running','waiting_config'].includes(task.status)){const cancel=document.createElement('button');cancel.textContent='停止';cancel.disabled=task.cancel_requested;cancel.onclick=async()=>{if(task.status==='running'&&!confirm('停止任务将重启这个工作空间的执行环境，文件与记录会保留。已经发生的外部操作无法撤销。'))return;try{await post('/api/cancel',{task_id:task.id});await refreshConversation();}catch(e){toast(e.message);}};actions.append(cancel);}
  if(task&&['failed','interrupted','cancelled'].includes(task.status)){const retry=document.createElement('button');retry.textContent='重新尝试';retry.onclick=async()=>{if(!confirm('请先检查已保存的文件与过程。重新尝试可能重复之前的外部操作，确定继续？'))return;try{await post('/api/retry',{task_id:task.id,idempotency_key:crypto.randomUUID()});await refreshConversation();}catch(e){toast(e.message);}};actions.append(retry);}
  footer.append(actions);if(task?.error){const note=document.createElement('div');note.className='message-task-note';note.textContent=task.error;footer.append(note);}return footer;
}
function mergeRecords(current,next,compare){const records=new Map(current.map(item=>[item.id,item]));for(const item of next)records.set(item.id,item);return [...records.values()].sort(compare);}
const byMessage=(a,b)=>a.id-b.id,byTask=(a,b)=>b.created_at-a.created_at||b.id.localeCompare(a.id);
function mergeTasks(next){const known=new Map(tasks.map(task=>[task.id,task]));for(const task of next){const previous=known.get(task.id);if(!previous||(task.updated_at||0)>=(previous.updated_at||0))known.set(task.id,task);}tasks=[...known.values()].sort(byTask);}
async function hydrateMessageTasks(messages,id,version,refreshActive=false){
  const known=new Map(tasks.map(task=>[task.id,task]));
  const wanted=[...new Set(messages.filter(message=>message.role==='user'&&message.task_id).map(message=>message.task_id))].filter(taskId=>!known.has(taskId)||(refreshActive&&['queued','running','waiting_config'].includes(known.get(taskId).status)));
  for(let offset=0;offset<wanted.length;offset+=200){const ids=wanted.slice(offset,offset+200).join(',');const page=await api('/api/tasks?page=1&conversation='+id+'&ids='+encodeURIComponent(ids));if(version!==chatVersion)return false;mergeTasks(page.items);}
  return version===chatVersion;
}
async function loadEarlierMessages(){
  if(!conversation||messageBefore===null||messageHistoryBusy)return;const id=conversation.id,version=chatVersion;messageHistoryBusy=true;const view=$('messages'),height=view.scrollHeight,top=view.scrollTop;
  try{const page=await api('/api/messages?page=1&conversation='+id+'&before='+messageBefore);if(version!==chatVersion)return;if(!await hydrateMessageTasks(page.items,id,version))return;messageItems=mergeRecords(messageItems,page.items,byMessage);messageBefore=page.next_cursor;renderMessages(messageItems);view.scrollTop=top+view.scrollHeight-height;}catch(e){toast(e.message);}finally{if(version===chatVersion)messageHistoryBusy=false;}
}
async function refreshConversation(){
  if(!conversation)return;const id=conversation.id,version=chatVersion;
  const messages=await api('/api/messages?page=1&conversation='+id+(messagesLoaded?'&after='+(messageItems.at(-1)?.id||0):''));if(version!==chatVersion)return;
  const next=mergeRecords(messageItems,messages.items,byMessage);if(!await hydrateMessageTasks(next,id,version,true))return;
  const initial=!messagesLoaded;if(initial){messageBefore=messages.next_cursor;messagesLoaded=true;}messageItems=mergeRecords(messageItems,messages.items,byMessage);renderMessages(messageItems);if(initial)$('messages').scrollTop=$('messages').scrollHeight;
}
async function poll() {if(!user||polling||document.visibilityState==='hidden')return; polling=true;try{await refreshConversation(); if(panel.startsWith('task:'))await taskEvents(panel.slice(5)); if(!configuration.configured){const c=await api('/api/status');configuration=c;updateConfig();}}catch(e){if(user)error('send-error','连接暂时中断，任务仍在服务器运行。恢复连接后将自动更新。');}finally{polling=false;}}
$('prompt').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('composer').requestSubmit();}};
$('composer').onsubmit=async event=>{
  event.preventDefault(); const prompt=$('prompt').value.trim(); if(!prompt)return; $('send').disabled=true; error('send-error','');
  try {if(!conversation){const c=await post('/api/conversations',{title:prompt.slice(0,26)});conversation=c;updateFilesButton();await loadConversations();localStorage.setItem('conversation:'+user.id,c.id);$('chat-title').textContent=c.title;}
    if(pendingPrompt!==prompt || pendingConversation!==conversation.id){pendingPrompt=prompt;pendingConversation=conversation.id;pendingKey=crypto.randomUUID();}
    await post('/api/tasks',{conversation_id:conversation.id,prompt,idempotency_key:pendingKey});pendingKey='';pendingPrompt='';pendingConversation='';$('prompt').value='';await refreshConversation();
  }catch(e){error('send-error',e.message);}finally{$('send').disabled=false;}
};
$('logout').onclick=async()=>{try{await post('/api/logout',{});showLogin();configuration=await api('/api/status');signup=false;authMode();}catch(e){toast(e.message);}};
function openPanel(title,kind) {panel=kind;$('details').hidden=false;$('panel-title').textContent=title;$('panel-content').replaceChildren();$('panel-content').scrollTop=0;}
$('panel-close').onclick=()=>{panel='';$('details').hidden=true;};
function eventText(e) {const d=e.payload;if(d?.response)return d.response;if(d?.content)return typeof d.content==='string'?d.content:JSON.stringify(d.content,null,2);if(d?.message)return d.message;return JSON.stringify(d,null,2);}
async function showTask(task){openPanel('任务过程','task:'+task.id);eventState={id:task.id,items:[],before:null,loaded:false,busy:false};const note=document.createElement('p');note.className='muted';note.textContent='执行过程保留在服务器，重新打开网页也可以继续查看。';$('panel-content').append(note);const area=document.createElement('div');area.id='task-events';$('panel-content').append(area);await taskEvents(task.id);}
function renderTaskEvents(state){if(eventState!==state||panel!=='task:'+state.id)return;const area=$('task-events');if(!area)return;const encoded=JSON.stringify([state.before,state.items]);if(area.dataset.events===encoded)return;const view=$('panel-content'),nearEnd=view.scrollHeight-view.scrollTop-view.clientHeight<100;area.dataset.events=encoded;area.replaceChildren();if(state.before!==null){const more=document.createElement('button');more.textContent='加载更早过程';more.onclick=()=>loadEarlierEvents(state);area.append(more);}for(const e of state.items){const item=document.createElement('div');item.className='event';const meta=document.createElement('small');meta.textContent=new Date(e.created_at*1000).toLocaleTimeString('zh-CN')+' · '+e.type;const body=document.createElement('div');body.textContent=eventText(e);item.append(meta,body);area.append(item);}if(nearEnd)view.scrollTop=view.scrollHeight;}
async function taskEvents(id){const state=eventState;if(!state||state.id!==id||state.busy)return;state.busy=true;try{const page=await api('/api/events?page=1&task='+id+(state.loaded?'&after='+(state.items.at(-1)?.id||0):''));if(eventState!==state)return;if(!state.loaded){state.before=page.next_cursor;state.loaded=true;}state.items=mergeRecords(state.items,page.items,byMessage);renderTaskEvents(state);}finally{state.busy=false;}}
async function loadEarlierEvents(state){if(eventState!==state||panel!=='task:'+state.id||state.before===null||state.busy)return;const view=$('panel-content'),height=view.scrollHeight,top=view.scrollTop;state.busy=true;try{const page=await api('/api/events?page=1&task='+state.id+'&before='+state.before);if(eventState!==state||panel!=='task:'+state.id)return;state.before=page.next_cursor;state.items=mergeRecords(state.items,page.items,byMessage);renderTaskEvents(state);view.scrollTop=top+view.scrollHeight-height;}catch(e){toast(e.message);}finally{state.busy=false;}}
function updateFilesButton(){const enabled=!!user&&!!conversation;$('files-toggle').disabled=!enabled;$('files-toggle').title=enabled?'管理当前对话的文件':'发送第一条消息后，管理这个对话的文件';}
function currentFiles(state){return filesState===state&&panel==='files'&&state.conversationId===conversation?.id&&state.version===chatVersion&&state.userId===user?.id&&state.session===csrfToken;}
function filesQuery(state){return 'conversation='+encodeURIComponent(state.conversationId)+'&path='+encodeURIComponent(state.path);}
$('files-toggle').onclick=()=>{filePath='';showFiles();};
async function showFiles(){
  if(!user||!conversation){updateFilesButton();return;}
  const state={conversationId:conversation.id,userId:user.id,session:csrfToken,version:chatVersion,path:filePath,items:[],cursor:null,busy:true};
  openPanel('当前对话文件','files');filesState=state;
  const heading=document.createElement('p');heading.className='muted file-conversation';heading.textContent=conversation.title||'当前对话';$('panel-content').append(heading);
  const path=document.createElement('div');path.className='file-path';path.textContent=state.path?'文件 / '+state.path:'文件 /';$('panel-content').append(path);
  if(state.path){const back=document.createElement('button');back.textContent='上一级';back.onclick=()=>{if(!currentFiles(state))return;filePath=state.path.split('/').slice(0,-1).join('/');showFiles();};$('panel-content').append(back);}
  const upload=document.createElement('label');upload.className='upload';upload.textContent='上传到当前对话（最大 8 MB）';const input=document.createElement('input');input.type='file';
  input.onchange=async()=>{
    const f=input.files[0];if(!f||!currentFiles(state))return;if(f.size>8*1024*1024)return toast('上传文件最大 8 MB');input.disabled=true;
    try{
      const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(f);});
      if(!currentFiles(state))return;
      await post('/api/files',{conversation_id:state.conversationId,path:state.path,filename:f.name,content});
      if(!currentFiles(state))return;toast('文件已保存到当前对话');await showFiles();
    }catch(e){if(currentFiles(state))toast(e.message);}finally{input.disabled=false;}
  };
  upload.append(input);$('panel-content').append(upload);const list=document.createElement('div');list.id='file-list';$('panel-content').append(list);
  try{const page=await api('/api/files?page=1&'+filesQuery(state));if(!currentFiles(state))return;state.items=page.items;state.cursor=page.next_cursor;renderFiles(state);}catch(e){if(currentFiles(state))toast(e.message);}finally{state.busy=false;}
}
function renderFiles(state){
  if(!currentFiles(state))return;const list=$('file-list');if(!list)return;list.replaceChildren();
  if(!state.items.length){const note=document.createElement('p');note.className='muted';note.textContent='这个对话还没有文件。你可以上传文件，或让 Agent 创建文件。';list.append(note);}
  for(const f of state.items){
    const row=document.createElement('div');row.className='file-row';const target=state.path?state.path+'/'+f.name:f.name;
    if(f.is_dir){const button=document.createElement('button');button.textContent='▣ '+f.name;button.onclick=()=>{if(!currentFiles(state))return;filePath=target;showFiles();};row.append(button);}
    else if(f.is_file){const link=document.createElement('a');link.textContent=f.name;link.href='/api/file?conversation='+encodeURIComponent(state.conversationId)+'&path='+encodeURIComponent(target);link.download=f.name;row.append(link);const size=document.createElement('small');size.textContent=f.size<1024?f.size+' B':f.size<1024*1024?(f.size/1024).toFixed(1)+' KB':f.size<1024*1024*1024?(f.size/(1024*1024)).toFixed(1)+' MB':(f.size/(1024*1024*1024)).toFixed(1)+' GB';row.append(size);}
    else{const text=document.createElement('span');text.textContent=f.name+'（链接或特殊文件）';row.append(text);}list.append(row);
  }
  if(state.cursor!==null){const more=document.createElement('button');more.textContent='加载更多文件';more.onclick=()=>loadMoreFiles(state);list.append(more);}
}
async function loadMoreFiles(state){
  if(!currentFiles(state)||state.cursor===null||state.busy)return;state.busy=true;
  try{const page=await api('/api/files?page=1&'+filesQuery(state)+'&cursor='+encodeURIComponent(state.cursor));if(!currentFiles(state))return;const items=new Map(state.items.map(item=>[item.name,item]));for(const item of page.items)items.set(item.name,item);state.items=[...items.values()];state.cursor=page.next_cursor;renderFiles(state);}catch(e){if(currentFiles(state))toast(e.message);}finally{state.busy=false;}
}
$('settings-toggle').onclick=showSettings;
async function showSettings(){openPanel('管理员设置','settings');try{const settings=await api('/api/settings');const form=document.createElement('form');form.className='settings-form';form.innerHTML='<span class="badge">'+(settings.configured?'模型已接入':'等待配置')+'</span><label>模型<select id="model"><option value="deepseek-v4-flash">DeepSeek V4 Flash</option><option value="deepseek-v4-pro">DeepSeek V4 Pro</option></select></label><label>DeepSeek API Key<input id="api-key" type="password" autocomplete="off" placeholder="'+(settings.configured?'已保存，留空则保留':'填写你的 API Key')+'"></label><p>密钥只保存在服务器。执行环境通过代理调用模型，用户无法读取这个密钥。</p><label><span><input id="signup-enable" type="checkbox"> 开放测试用户注册</span></label><p>开启后，持有网站地址的人可以创建账户并使用模型额度。小范围测试结束后可关闭。</p><p id="settings-error" class="error" hidden></p><button class="primary" type="submit">保存设置</button><hr><p>执行并发：1 · 任务在持久化队列中排队</p><p>当前测试版提供持久化 Linux 工作空间。文件与记录跨重启保留；主机故障后，运行中的任务会标记为中断，等待检查。</p>';
    $('panel-content').append(form);$('model').value=settings.model;$('signup-enable').checked=settings.signup_enabled;form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector('button');button.disabled=true;error('settings-error','');try{const result=await post('/api/settings',{model:$('model').value,api_key:$('api-key').value,signup_enabled:$('signup-enable').checked});$('api-key').value='';configuration=result.config;updateConfig();toast('设置已保存');await showSettings();}catch(e){error('settings-error',e.message);}finally{button.disabled=false;}};
    const heading=document.createElement('h3');heading.textContent='测试用户';$('panel-content').append(heading);for(const u of settings.users){const p=document.createElement('p');p.textContent=u.username+(u.is_admin?' · 管理员':'');$('panel-content').append(p);}
  }catch(e){toast(e.message);}}
(async()=>{try{configuration=await api('/api/status');authMode();try{await enter(await api('/api/me'));}catch(e){if(!user && !e.message.includes('请先登录'))error('auth-error',e.message);}}catch(e){error('auth-error','服务暂时无法连接，请稍后刷新。');}})();
