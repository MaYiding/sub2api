'use strict';
let csrf = '', timer;
const $ = id => document.getElementById(id);
const stateNames = {provisioning:'正在配置',ready:'配置可领取',active:'链路已验证',disabling:'正在停用',disabled:'已停用'};
function message(text) { $('notice').textContent = text; }
function node(tag,text,cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n; }
const errorLabels={'raw topic capacity budget exhausted':'原始日志缓冲额度已分配完，请先调整容量预算。','configuration is not ready or download window expired; rotate credentials':'配置尚未准备好或领取窗口已过期；过期时请生成下一代凭据。','source has an unfinished operation':'该来源仍有后台操作，请稍后重试。','finish the existing rotation first':'请先确认或取消当前轮换。','latest credentials need source acknowledgement and archived probe':'新配置需要完成心跳确认和 HDD 回放验证后才能确认轮换。','Administrator authentication required':'请先登录管理员账号。','Invalid credentials':'账号或密码不正确。','CSRF validation failed':'会话校验失败，请刷新页面后重试。'};
function date(value) { return value ? new Date(value*1000).toLocaleString() : '尚无记录'; }
function utcDate(value){return value?new Date(value.replace(' ','T')+'Z').toLocaleString():'尚无记录';}
function bytes(value) { return ((value||0)/1024**3).toFixed(2)+' GiB'; }
async function api(path,options={}) {
  const headers={'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers};
  const response=await fetch(path,{credentials:'same-origin',cache:'no-store',...options,headers});
  if(!response.ok) { let detail='请求失败';try{detail=(await response.json()).detail||detail;}catch{} if(response.status===401)showLogin();throw new Error(errorLabels[detail]||detail); }
  return response;
}
function showLogin(){clearInterval(timer);$('login-panel').hidden=false;$('workspace').hidden=true;$('logout').hidden=true;}
async function showWorkspace(){ $('login-panel').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;await refresh();clearInterval(timer);timer=setInterval(()=>refresh().catch(e=>message(e.message)),10000); }
async function mutate(path,data={}) {
  const fingerprint=path+JSON.stringify(data), key='ai-log-pending:'+fingerprint;
  let id=sessionStorage.getItem(key);if(!id){id=crypto.randomUUID();sessionStorage.setItem(key,id);}
  const result=await (await api(path,{method:'POST',headers:{'Idempotency-Key':id},body:JSON.stringify(data)})).json();
  sessionStorage.removeItem(key);return result;
}
function button(text,callback,danger=false){const n=node('button',text,danger?'danger':'');n.addEventListener('click',async()=>{n.disabled=true;try{await callback();}catch(e){message(e.message);}finally{n.disabled=false;}});return n;}
function fact(parent,label,value){const n=node('div');n.append(node('span',label+'：'),node('strong',String(value)));parent.append(n);}
async function download(source){const response=await api('/control/v1/sources/'+source.id+'/bundle',{method:'POST',body:'{}'});const blob=await response.blob();const url=URL.createObjectURL(blob);const a=node('a');a.href=url;a.download=source.id+'-shipper.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('配置包已下载。请安全转移到对应服务器，设置 0600 权限，不要发送到聊天或提交到 Git。');}
function render(source,now){
  const card=node('article',undefined,'source'),head=node('div',undefined,'source-head');head.append(node('h3',source.name),node('span',stateNames[source.state]||source.state,'badge '+source.state));card.append(head);
  const facts=node('div',undefined,'facts');fact(facts,'固定来源 ID',source.id);fact(facts,'地域 / 环境',(source.region||'未填写')+' / '+source.environment);fact(facts,'最近心跳',date(source.heartbeat_at));fact(facts,'源端积压',bytes(source.heartbeat.pending_bytes));fact(facts,'原始 Topic',source.topic);fact(facts,'缓冲额度',source.retention_gib+' GiB');fact(facts,'上报主机（客户端报告）',source.heartbeat.hostname||'尚未接入');fact(facts,'积压最老时间',(source.heartbeat.oldest_age_seconds||0)+' 秒');card.append(facts);
  const latest=source.credentials.find(c=>c.state!=='revoked'),fresh=source.heartbeat_at&&now-source.heartbeat_at<=120;
  const checks=node('div',undefined,'checks');for(const [text,ok] of [['配置已准备',latest&&['ready','active'].includes(latest.state)],['心跳在线',fresh],['源端 Kafka 发送确认',source.heartbeat.last_delivery_at>0],['HDD 回放已验证',latest&&latest.verified_at]])checks.append(node('div',(ok?'✓ ':'○ ')+text,'check'+(ok?' ok':'')));card.append(checks);
  const observed=node('p',undefined,'hint');const archive=source.observed.archive,clean=source.observed.clean;observed.textContent='中央观察 · 最近清洗：'+utcDate(clean?.last_seen)+' · 最近归档：'+utcDate(archive?.last_seen);card.append(observed);
  const jobs=source.jobs.filter(j=>['pending','running'].includes(j.state));if(jobs.length)card.append(node('p','配置任务处理中'+(jobs[0].error_class?'，上次未完成，系统正在重试':'')+'。不会在后台重复创建来源。','hint'));
  const creds=node('div',undefined,'credentials');for(const c of source.credentials)creds.append(node('p','第 '+c.generation+' 代 · '+c.principal+' · '+c.state+' · 创建于 '+date(c.created_at),'hint'));card.append(creds);
  const actions=node('div',undefined,'actions');
  if(latest&&['ready','active'].includes(latest.state))actions.append(button('下载本站配置',()=>download(source)));
  actions.append(button('修改名称 / 备注',async()=>{const name=prompt('来源名称',source.name);if(name===null)return;const region=prompt('地域或服务器备注',source.region);if(region===null)return;await mutate('/control/v1/sources/'+source.id+'/rename',{name,region});message('名称更新已提交，来源 ID 保持不变。');await refresh();}));
  if(!['disabled','disabling'].includes(source.state)){
    actions.append(button('生成下一代凭据',async()=>{if(!confirm('生成新配置，旧凭据暂时继续有效。完成安装和 HDD 探测后再确认轮换。'))return;await mutate('/control/v1/sources/'+source.id+'/rotations');message('新凭据正在准备，请稍后下载本站配置。');await refresh();}));
    if(latest?.verified_at&&source.credentials.filter(c=>c.state!=='revoked').length>1)actions.append(button('确认轮换',async()=>{if(!confirm('确认本站已使用最新配置，并撤销该站点旧凭据？'))return;await mutate('/control/v1/sources/'+source.id+'/confirm-rotation',{credential_id:latest.id});message('撤销旧凭据的任务已提交。');await refresh();}));
    if(source.credentials.filter(c=>c.state!=='revoked').length===2)actions.append(button('取消本次轮换',async()=>{if(!confirm('撤销最新一代凭据并保留旧配置。若已安装新配置，请先恢复源端旧配置。'))return;await mutate('/control/v1/sources/'+source.id+'/cancel-rotation');message('取消轮换已提交。');await refresh();}));
    actions.append(button('停用上报',async()=>{if(!confirm('停用将拒绝这台服务器所有凭据的写入，源端 WAL 会积压。历史日志保留，其他来源不受影响。'))return;await mutate('/control/v1/sources/'+source.id+'/disable');message('停用已提交，正在撤销所有凭据。');await refresh();},true));
  }else if(source.state==='disabled'){actions.append(button('重新启用',async()=>{await mutate('/control/v1/sources/'+source.id+'/enable');message('正在生成新凭据，来源 ID 和历史日志保持不变。');await refresh();}));}
  card.append(actions);return card;
}
async function refresh(){const data=await (await api('/control/v1/sources')).json();$('sources').replaceChildren(...data.sources.map(s=>render(s,data.server_time)));if(!data.sources.length)$('sources').append(node('section','还没有来源，请先注册。'));$('capacity').textContent='原始日志总缓冲预算 '+data.raw_budget_gib+' GiB，已分配 '+data.sources.reduce((v,s)=>v+s.retention_gib,0)+' GiB。';}
$('login').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target);try{const data=await (await api('/control/v1/login',{method:'POST',body:JSON.stringify({username:f.get('username'),password:f.get('password')})})).json();csrf=data.csrf;e.target.elements.password.value='';message('');await showWorkspace();}catch(error){message(error.message);}});
$('create').addEventListener('submit',async e=>{e.preventDefault();const f=Object.fromEntries(new FormData(e.target));f.retention_gib=Number(f.retention_gib);try{await mutate('/control/v1/sources',f);message('来源已登记，正在配置独立账号和 Topic。');e.target.reset();await refresh();}catch(error){message(error.message);}});
$('refresh').onclick=()=>refresh().catch(e=>message(e.message));
$('logout').onclick=async()=>{try{await api('/control/v1/logout',{method:'POST'});}finally{csrf='';showLogin();}};
$('load-audit').onclick=async()=>{try{const data=await (await api('/control/v1/audit')).json();$('audit').textContent=data.events.map(e=>date(e.at)+' '+e.actor+' '+e.action+' '+e.target+' '+e.detail).join('\n');}catch(e){message(e.message);}};
(async()=>{try{csrf=(await (await api('/control/v1/session')).json()).csrf;await showWorkspace();}catch{showLogin();}})();
