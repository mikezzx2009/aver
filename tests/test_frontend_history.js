'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const elements=new Map(),listeners=new Map(),intervals=new Map();let nextTimer=0;
class Element{
  constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.parent=null;this.clientHeight=100;this._scrollTop=0;this._text='';this.value='';}
  set id(value){this._id=value;elements.set(value,this);}get id(){return this._id;}
  set textContent(value){this._text=String(value);this.children=[];}get textContent(){return this._text||this.children.map(item=>item.textContent).join('');}
  set innerHTML(value){this._html=value;this.replaceChildren();}get innerHTML(){return this._html||'';}
  append(...items){for(const item of items){item.parent=this;this.children.push(item);}}
  replaceChildren(...items){for(const child of this.children)child.parent=null;this.children=[];this._text='';this.append(...items);}
  get scrollHeight(){return this.children.length*20;}get scrollTop(){return this._scrollTop;}set scrollTop(value){this._scrollTop=Math.max(0,Math.min(value,Math.max(0,this.scrollHeight-this.clientHeight)));}
  matches(selector){return selector==='[data-message-id]'?!!this.dataset.messageId:['button','a'].includes(selector)?this.tagName===selector:selector.startsWith('.')?(this.className||'').split(' ').includes(selector.slice(1)):false;}
  querySelectorAll(selector){return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]);}
  getBoundingClientRect(){let top=0,node=this;while(node.parent){top+=node.parent.children.indexOf(node)*20-node.parent.scrollTop;node=node.parent;}return {top,bottom:top+20};}
}
const get=id=>{if(!elements.has(id)){const element=new Element();element.id=id;}return elements.get(id);};
const document={getElementById:get,createElement:tag=>new Element(tag),addEventListener(type,callback){if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(callback);},visibilityState:'visible'};
const context=vm.createContext({console,URLSearchParams,URL,AbortController,crypto:require('node:crypto').webcrypto,document,
  location:{search:''},history:{replaceState(){}},localStorage:{getItem(){return null;},setItem(){},removeItem(){}},setTimeout(){},clearTimeout(){},
  setInterval(callback,ms){const id=++nextTimer;intervals.set(id,{callback,ms});return id;},clearInterval(id){intervals.delete(id);},confirm:()=>true,
  fetch:async url=>({ok:url==='/api/status',status:url==='/api/status'?200:401,json:async()=>url==='/api/status'?{needs_bootstrap:false,signup_enabled:false,configured:false}:{error:'请先登录'}})});
const run=code=>vm.runInContext(code,context),tick=()=>new Promise(resolve=>setImmediate(resolve));
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),context);
context.realAPI=run('api');
const taskId=id=>id.toString(16).padStart(32,'0'),message=id=>({id,role:'user',task_id:taskId(id),content:'Message '+id,created_at:id});
let taskStatus='queued',taskRevision=1,messageLive=false;
const task=id=>({id:taskId(id),prompt:'Task '+id,status:taskStatus,updated_at:taskRevision,created_at:id,cancel_requested:false,error:taskStatus==='failed'?'测试失败':null});
const event=id=>({id,created_at:id,type:'output',payload:{content:'Progress '+id}}),range=(start,end,fn)=>Array.from({length:end-start+1},(_,i)=>fn(start+i));
const articleFor=id=>get('messages').querySelectorAll('[data-message-id]').find(item=>item.dataset.messageId===String(id));
const stats=()=>({cpu_percent:12.5,cpu_count:2,memory:{used_bytes:700*1024**2,total_bytes:2*1024**3,percent:34.2},disk:{used_bytes:20*1024**3,total_bytes:40*1024**3,percent:50},load_average:[0.3,0.2,0.1],collected_at:Date.now()/1000,sample_seconds:5});
let taskRequests=[];
context.fixtureAPI=async request=>{const url=new URL(request,'http://localhost'),q=url.searchParams;
  if(url.pathname==='/api/messages'){
    if(q.has('before'))return {items:range(1,100,message),has_more:false,next_cursor:null};
    if(q.has('after'))return {items:messageLive?[message(201)]:[],has_more:false,next_cursor:null};
    return {items:range(101,200,message),has_more:true,next_cursor:101};
  }
  if(url.pathname==='/api/tasks'){assert(q.has('ids'),'Message tasks must hydrate by IDs, not an unrelated latest-50 list');const ids=q.get('ids').split(',');assert(ids.length<=200);taskRequests.push(ids);return {items:ids.map(id=>task(parseInt(id,16))),has_more:false,next_cursor:null};}
  if(url.pathname==='/api/events'){
    if(q.has('before'))return {items:range(401,500,event),has_more:true,next_cursor:401};
    if(q.has('after'))return {items:[event(601)],has_more:false,next_cursor:null};
    return {items:range(501,600,event),has_more:true,next_cursor:501};
  }
  throw new Error('Unexpected API path '+request);
};
(async()=>{
  await tick();run("api=fixtureAPI;user={id:'user',username:'test'};conversation={id:'conversation'};chatVersion=1;");
  context.publicSite='http://203.0.113.10/sites/'+taskId(999)+'/';
  run("messageContent($('unconfigured-origin-test'), '访问：'+publicSite)");
  assert.equal(get('unconfigured-origin-test').querySelectorAll('a').length,0);
  run("configuration.site_base_url='http://203.0.113.10'");
  run("messageContent($('link-test'), '访问：'+publicSite+'。 <script>alert(1)</script> http://127.0.0.1:8000')");
  const publishedLinks=get('link-test').querySelectorAll('a');assert.equal(publishedLinks.length,1);
  assert.equal(publishedLinks[0].href,context.publicSite);assert.equal(publishedLinks[0].rel,'noopener noreferrer');
  assert(get('link-test').textContent.includes('<script>alert(1)</script>'),'Ordinary message text must remain escaped text');
  run("messageContent($('invalid-link-test'), 'http://203.0.113.10/api/me http://203.0.113.10@evil.example/sites/'+publicSite.split('/')[4]+'/')");
  assert.equal(get('invalid-link-test').querySelectorAll('a').length,0);
  run("messageContent($('short-link-test'), '打开 http://203.0.113.10/ruler/，资源 http://203.0.113.10/a1b2c3/assets/app.js?version=2#preview；历史 '+publicSite+'assets/style.css。 <img src=x onerror=alert(1)>')");
  const shortLinks=get('short-link-test').querySelectorAll('a');assert.deepEqual(shortLinks.map(link=>link.href),['http://203.0.113.10/ruler/','http://203.0.113.10/a1b2c3/assets/app.js?version=2#preview',context.publicSite+'assets/style.css']);
  assert(shortLinks.every(link=>link.target==='_blank'&&link.rel==='noopener noreferrer'));assert(get('short-link-test').textContent.includes('<img src=x onerror=alert(1)>'),'HTML in short-link messages must remain plain text');
  run("messageContent($('invalid-short-link-test'), 'http://evil.example/ruler/ http://203.0.113.10@evil.example/ruler/ http://user:pass@203.0.113.10/ruler/ http://203.0.113.10/api/me http://203.0.113.10/private/data http://203.0.113.10/publication-records/site.json http://203.0.113.10/workspace/ http://203.0.113.10/settings/ http://203.0.113.10/login/ http://203.0.113.10/logout/ http://203.0.113.10/health/ http://203.0.113.10/favicon/ http://203.0.113.10/abc/ http://203.0.113.10/abcdefghijk/ http://203.0.113.10/Ruler/ http://203.0.113.10/ruler/../api/me')");
  assert.equal(get('invalid-short-link-test').querySelectorAll('a').length,0,'Foreign, credentialed, private/API and invalid-slug URLs must stay plain text');
  run("messageContent($('reserved-route-test'), 'http://203.0.113.10/signup/ http://203.0.113.10/status/ http://203.0.113.10/config/ http://203.0.113.10/published/')");assert.equal(get('reserved-route-test').querySelectorAll('a').length,0);
  run("configuration.site_base_url='https://preview.example:8443';messageContent($('configured-origin-test'), 'https://preview.example:8443/ruler/ https://preview.example/ruler/ http://preview.example:8443/ruler/ http://203.0.113.10/ruler/');delete configuration.site_base_url;");
  assert.deepEqual(get('configured-origin-test').querySelectorAll('a').map(link=>link.href),['https://preview.example:8443/ruler/'],'Only the configured public origin, including protocol and port, may be linked');
  await run('refreshConversation()');assert.equal(run('messageItems[0].id'),101);assert.equal(run('tasks.length'),100);
  assert.equal(get('messages').scrollTop,get('messages').scrollHeight-get('messages').clientHeight);
  assert.equal(get('messages').children[0].textContent,'加载更早消息');
  assert.equal(articleFor(101).querySelectorAll('.status')[0].textContent,'排队中');
  assert.deepEqual(articleFor(101).querySelectorAll('button').map(button=>button.textContent),['过程','停止']);
  get('messages').scrollTop=0;const previousHeight=get('messages').scrollHeight;
  await run('loadEarlierMessages()');assert.equal(get('messages').scrollTop,get('messages').scrollHeight-previousHeight);
  assert.equal(run('messageItems.length'),200);assert.equal(run('tasks.length'),200);
  assert(taskRequests.some(ids=>ids.includes(taskId(1))),'Old messages beyond the latest 50 tasks must fetch their own task state');
  assert.equal(articleFor(1).querySelectorAll('.status')[0].textContent,'排队中');
  get('messages').scrollTop=600;taskStatus='running';taskRevision=2;await run('refreshConversation()');
  assert.equal(run('messageItems.length'),200);assert.equal(articleFor(1).querySelectorAll('.status')[0].textContent,'执行中','Status must update without any new message');
  assert.equal(get('messages').scrollTop,600,'Status refresh must preserve the reading anchor');
  taskStatus='failed';taskRevision=3;await run('refreshConversation()');
  assert.deepEqual(articleFor(1).querySelectorAll('button').map(button=>button.textContent),['过程','重新尝试']);
  messageLive=true;await run('refreshConversation()');assert.equal(run('messageItems.length'),201);assert.equal(run('messageItems[0].id'),1);
  assert.equal(run('messageItems.at(-1).id'),201);assert.equal(articleFor(201).querySelectorAll('.status')[0].textContent,'失败');
  assert(!fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8').includes('active-tasks'));
  await run("showTask({id:'task'})");await run('loadEarlierEvents(eventState)');await run("taskEvents('task')");
  assert.equal(run('eventState.items[0].id'),401);assert.equal(run('eventState.items.at(-1).id'),601);assert.equal(run('eventState.items.length'),201);

  let resolveOldDirectory;context.fixtureAPI=async request=>{const url=new URL(request,'http://localhost');if(url.pathname==='/api/files'){if(url.searchParams.get('path')==='old')return new Promise(resolve=>{resolveOldDirectory=resolve;});return {items:[{name:'new.txt',is_file:true,is_dir:false,size:123}],has_more:false,next_cursor:null};}throw new Error(request);};run('api=fixtureAPI');
  const oldDirectory=run("filePath='old';showFiles()");await run("filePath='new';showFiles()");resolveOldDirectory({items:[{name:'old.txt',is_file:true,is_dir:false,size:123}],has_more:false,next_cursor:null});await oldDirectory;assert.equal(run('filesState.path'),'new');assert.equal(run('filesState.items[0].name'),'new.txt');
  const fileRequests=[],uploads=[];let resolveOldFiles,resolveFilePage,resolveUpload,reader,delayListing=false,delayUpload=false;
  context.FileReader=class{readAsDataURL(){reader=this;}};
  context.fixtureAPI=async(request,options={})=>{
    const url=new URL(request,'http://localhost'),q=url.searchParams;
    if(url.pathname==='/api/messages')return {items:[],has_more:false,next_cursor:null};
    if(url.pathname!=='/api/files')throw new Error(request);
    if(options.method==='POST'){uploads.push(JSON.parse(options.body));if(delayUpload)return new Promise(resolve=>{resolveUpload=resolve;});return {ok:true};}
    assert(q.get('conversation'),'Every file listing must carry its captured conversation');fileRequests.push({conversation:q.get('conversation'),path:q.get('path'),cursor:q.get('cursor')});
    if(q.get('cursor'))return new Promise(resolve=>{resolveFilePage=resolve;});
    if(delayListing&&q.get('conversation')==='file-a')return new Promise(resolve=>{resolveOldFiles=resolve;});
    return {items:[{name:q.get('conversation')+'.txt',is_file:true,is_dir:false,size:123}],has_more:q.get('conversation')==='file-a',next_cursor:q.get('conversation')==='file-a'?'next':null};
  };run('api=fixtureAPI');
  await run("selectConversation({id:'file-a',title:'第一项工作'})");assert.equal(get('files-toggle').disabled,false);await run("filePath='project';showFiles()");
  assert.equal(get('panel-title').textContent,'当前对话文件');assert(get('panel-content').textContent.includes('第一项工作'));assert(!get('panel-content').textContent.includes('/workspace/'));
  assert.equal(get('file-list').querySelectorAll('a')[0].href,'/api/file?conversation=file-a&path=project%2Ffile-a.txt');
  assert.deepEqual(fileRequests.at(-1),{conversation:'file-a',path:'project',cursor:null});
  const oldPage=run('loadMoreFiles(filesState)');await run("selectConversation({id:'file-b',title:'第二项工作'})");assert.equal(run('filesState'),null);assert.equal(run('filePath'),'');assert.equal(get('details').hidden,true);
  await run('showFiles()');resolveFilePage({items:[{name:'old-page.txt',is_file:true,is_dir:false,size:123}],has_more:false,next_cursor:null});await oldPage;
  assert.equal(run('filesState.conversationId'),'file-b');assert.equal(run('filesState.items[0].name'),'file-b.txt');assert(!get('file-list').textContent.includes('old-page'));
  delayListing=true;await run("selectConversation({id:'file-a',title:'第一项工作'})");const oldFiles=run('showFiles()');await run("selectConversation({id:'file-b',title:'第二项工作'})");await run('showFiles()');resolveOldFiles({items:[{name:'late-a.txt',is_file:true,is_dir:false,size:123}],has_more:false,next_cursor:null});await oldFiles;
  assert.equal(run('filesState.conversationId'),'file-b');assert(!get('file-list').textContent.includes('late-a'));delayListing=false;
  const uploadInput=()=>get('panel-content').children.find(element=>element.tagName==='label').children[0];
  await run("selectConversation({id:'file-a',title:'第一项工作'})");await run("filePath='project';showFiles()");let input=uploadInput();input.files=[{name:'plan.txt',size:3}];const cancelledRead=input.onchange();
  await run("selectConversation({id:'file-b',title:'第二项工作'})");await run('showFiles()');reader.result='data:text/plain;base64,YWJj';reader.onload();await cancelledRead;assert.equal(uploads.length,0,'Switching conversations during file reading must prevent upload');
  await run("selectConversation({id:'file-a',title:'第一项工作'})");await run("filePath='project';showFiles()");input=uploadInput();input.files=[{name:'plan.txt',size:3}];delayUpload=true;const pendingUpload=input.onchange();reader.result='data:text/plain;base64,YWJj';reader.onload();await tick();
  assert.deepEqual(uploads[0],{conversation_id:'file-a',path:'project',filename:'plan.txt',content:'YWJj'});
  await run("selectConversation({id:'file-b',title:'第二项工作'})");await run('showFiles()');get('toast').textContent='current';resolveUpload({ok:true});await pendingUpload;
  assert.equal(run('filesState.conversationId'),'file-b');assert.equal(get('toast').textContent,'current','A completed upload in another conversation must not replace current UI');delayUpload=false;
  input=uploadInput();input.files=[{name:'saved.txt',size:3}];const savedUpload=input.onchange();reader.result='data:text/plain;base64,YWJj';reader.onload();await savedUpload;
  assert.deepEqual(uploads[1],{conversation_id:'file-b',path:'',filename:'saved.txt',content:'YWJj'});assert.equal(get('toast').textContent,'文件已保存到当前对话');
  const requestCount=fileRequests.length;await run('selectConversation(null)');await run('showFiles()');assert.equal(get('files-toggle').disabled,true);assert.equal(fileRequests.length,requestCount,'A new conversation must not open a shared file directory');
  let resolveOldMessages;context.fixtureAPI=async request=>{if(request.startsWith('/api/messages'))return new Promise(resolve=>{resolveOldMessages=resolve;});throw new Error(request);};run('api=fixtureAPI');
  const loadingConversation=run("selectConversation({id:'other',title:'Other'})");assert.equal(get('messages').children.length,1);assert.equal(get('messages').children[0].textContent,'正在加载对话…');run('showLogin()');resolveOldMessages({items:[message(999)],has_more:false,next_cursor:null});await loadingConversation;assert.equal(get('messages').children.length,0);assert.equal(run('tasks.length'),0);assert.equal(run('filesState'),null);

  let statsCalls=0,statsMode='ok';context.fixtureAPI=async request=>{if(request==='/api/conversations')return [];if(request==='/api/server-stats'){statsCalls++;if(statsMode==='fail')throw new Error('offline');return statsMode==='stale'?{...stats(),collected_at:Date.now()/1000-60}:stats();}throw new Error(request);};run('api=fixtureAPI');
  await run("enter({user:{id:'second',username:'second',is_admin:false},csrf:'second-session',config:{configured:true}})");await tick();
  assert.equal(run('conversation'),null);assert.equal(statsCalls,1,'Login must fetch server stats even without a conversation');assert.equal(get('stats-cpu').textContent,'12.5%');
  assert.equal(get('stats-disk-size').textContent,'20.0 / 40.0 GiB');assert(get('stats-load').title.includes('2 个 CPU 核心'));
  context.partialStats={...stats(),memory:null,cpu_count:null,load_average:[null,0.2,0.1]};run('renderServerStats(partialStats)');
  assert.equal(get('stats-cpu').textContent,'12.5%');assert.equal(get('stats-memory').textContent,'—');assert.equal(get('stats-memory-size').textContent,'');assert.equal(get('stats-disk').textContent,'50.0%');assert.equal(get('stats-load').textContent,'—');
  context.partialStats={...stats(),cpu_percent:null,disk:null};run('renderServerStats(partialStats)');assert.equal(get('stats-cpu').textContent,'采样中');assert.equal(get('stats-memory').textContent,'34.2%');assert.equal(get('stats-disk').textContent,'—');assert.equal(get('stats-load').textContent,'0.30');
  assert.equal([...intervals.values()].filter(item=>item.ms===5000).length,1);
  document.visibilityState='hidden';for(const callback of listeners.get('visibilitychange'))callback();
  assert.equal([...intervals.values()].filter(item=>item.ms===5000).length,0);assert.equal(get('stats-cpu').textContent,'—');await run('refreshServerStats()');assert.equal(statsCalls,1);
  document.visibilityState='visible';for(const callback of listeners.get('visibilitychange'))callback();await tick();assert.equal(statsCalls,2);
  statsMode='fail';await run('refreshServerStats()');assert.equal(get('stats-cpu').textContent,'—');assert.equal(get('stats-memory-size').textContent,'');assert.equal(get('stats-status').textContent,'暂不可用');
  statsMode='stale';await run('refreshServerStats()');assert.equal(get('stats-status').textContent,'数据已过期');assert.equal(get('stats-cpu').textContent,'—');
  let oldResolve,newResolve,oldSignal;context.fixtureAPI=async(request,options)=>{if(request==='/api/server-stats')return new Promise(resolve=>{if(!oldResolve){oldResolve=resolve;oldSignal=options.signal;}else newResolve=resolve;});throw new Error(request);};run('api=fixtureAPI');
  const oldStats=run('refreshServerStats()');run('showLogin()');assert(oldSignal.aborted);assert.equal(get('stats-cpu').textContent,'—');
  run("user={id:'third',username:'third'};startServerStats()");oldResolve({...stats(),cpu_percent:99});await oldStats;assert.equal(get('stats-cpu').textContent,'—','A late response from an old login must not populate a new session');newResolve(stats());await tick();assert.equal(get('stats-cpu').textContent,'12.5%');run('showLogin()');assert.equal([...intervals.values()].filter(item=>item.ms===5000).length,0);

  let stale401;context.fetch=async()=>new Promise(resolve=>{stale401=resolve;});run("api=realAPI;user={id:'old'};csrfToken='old-token';");const unauthorized=run("api('/api/server-stats')").catch(()=>{});run("user={id:'new'};csrfToken='new-token';");stale401({ok:false,status:401,json:async()=>({error:'请先登录'})});await unauthorized;assert.equal(run('user.id'),'new','An old session 401 must not log out a freshly authenticated account');
  console.log('Inline task state/history, reading anchors, server-stats lifecycle and account isolation checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
