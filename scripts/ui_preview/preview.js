/* Loaded only by server.py. Demo data never reaches an application backend. */
(() => {
 const now = Math.floor(Date.now()/1000);
 const models = ['gemini-2.5-pro','claude-sonnet-4-5','gemini-2.5-flash','gemini-3-pro-image'];
 let keys = ['研发团队 · 主应用','产品体验环境','设计工作室','合作伙伴接口','自动化测试'].map((name,i)=>({
  id:`preview-${i}`,name,kind:'managed',status:i===3?'disabled':'active',effective_status:i===3?'disabled':i===4?'quota_exhausted':'active',key_prefix:`0000000${i}`,
  secret_available:true,currency:'CNY',quota_amount:i===2?null:'200.00',quota_used:i===4?'200.00':(47.26+i*21.37).toFixed(2),quota_remaining:i===2?null:i===4?'0.00':(152.74-i*21.37).toFixed(2),
  quota_tokens_used:17850000+i*3641200,reset_mode:i%2?'manual':'monthly',expires_at:i===1?now+86400*30:null,claude_credential_name:`studio-${i%3}.json`,allowed_model:'',prompt_fusion_mode:'inherit',limit_enabled:false
 }));
 const accounts = ['engineering','studio','research'].map((n,i)=>({filename:`studio-${i}.json`,user_email:`${n}@example.com`,email:`${n}@example.com`,disabled:false,tier:i===1?'ultra':'pro',last_success:now-60*i,error_codes:[],preview:false}));
 const stats = accounts.flatMap((a,i)=>models.map((model,j)=>({...a,credential_name:a.filename,model_name:model,model,success_count:1327-i*216-j*139,failed_count:7+i+j,upstream_total_seconds:1926,upstream_timed_count:713,gateway_total_seconds:2337,gateway_timed_count:713})));
 const rows = accounts.flatMap((a,i)=>models.slice(0,3).map((model,j)=>({...a,group_key:a.filename,credential_name:a.filename,model_name:model,top_model:model,api_key_name:keys[i].name,api_key_id:keys[i].id,total:713-i*121-j*83,success_count:706-i*121-j*83,failed_count:7,success_rate:99.1,avg_gateway_seconds:3.28,avg_upstream_seconds:2.7,tps:42.7,input_tokens:483260,output_tokens:132470,total_tokens:615730,total_cost:'3.72',currency:'CNY',last_called_at:now-i*23,last_success:true,last_status_code:200,task_type:'chat',channel:'antigravity'})));
 const records = rows.map((r,i)=>({...r,id:i+1,request_id:`demo-request-${i}`,created_at:now-i*37,success:i!==3,status_code:i===3?429:200,gateway_response_seconds:3.28,upstream_response_seconds:2.7,protocol:'openai',transport:'stream'}));
 let config = {host:'127.0.0.1',port:7861,credentials_dir:'./creds',log_level:'INFO',retry_times:3,request_timeout:120,antigravity_request_concurrency:3};
 const json = (data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
 window.fetch = async (input,options={}) => {
  if(options.signal?.aborted) throw new DOMException('Aborted','AbortError');
  const url = new URL(typeof input==='string'?input:input.url,location.href), p=url.pathname;
  const method=options.method||'GET';
  let body={};try{body=JSON.parse(options.body||'{}')}catch{}
  if(p==='/version/info')return json({success:true,version:'preview',full_hash:'local-ui-preview',message:'本地界面预览',date:'2026-11-27',has_update:false});
  if(p==='/config/get')return json({config,env_locked:[],account_usage_defaults:{},capture_schema:{}});
  if(p==='/config/save'){config={...config,...body.config};return json({success:true,message:'演示配置已保存，刷新页面恢复'});}
  if(p==='/api-keys/accounts')return json({items:accounts});
  if(p==='/api-keys/models')return json({items:models});
  if(p==='/api-keys/lookup'){const i=keys.find(k=>body.api_key===`gcli_${k.key_prefix}_preview-only`);return json({id:i?.id||null});}
  if(p==='/api-keys' && method==='POST'){const id=`preview-${keys.length}`;const key={...keys[0],...body,id,key_prefix:String(keys.length).padStart(8,'0'),quota_used:'0',quota_remaining:body.quota_amount,quota_tokens_used:0};keys.unshift(key);return json({item:key},201);}
  if(p==='/api-keys')return json({items:keys,total:keys.length,page:1,page_size:100});
  if(p==='/api-keys/batch-limits')return json({updated:body.key_ids.length});
  if(p.startsWith('/api-keys/')){
   const k=keys.find(k=>k.id===p.split('/')[2]);if(!k)return json({detail:'演示密钥不存在'},404);
   if(p.endsWith('/secret'))return json({api_key:`gcli_${k.key_prefix}_preview-only`});
   if(method==='PATCH'){Object.assign(k,body);k.effective_status=k.status;}
   if(method==='DELETE'){k.status='revoked';k.effective_status='revoked';}
   if(p.endsWith('/quota-resets')){k.quota_used='0';k.quota_tokens_used=0;k.quota_remaining=k.quota_amount;}
   return json(k);
  }
  if(p==='/creds/status')return json({items:accounts,total:accounts.length,stats:{total:3,normal:3,disabled:0}});
  if(p==='/creds/model-stats')return json({stats});
  if(p==='/antigravity/warmup')return json({accounts:{},ip_groups:{},defaults:{enabled:false}});
  if(p==='/proxy-groups')return json({items:[],groups:[]});
  if(p.includes('refresh-import-batches'))return json({batches:[],items:[]});
  if(p==='/call-monitor/overview')return json({rows,counters:{accounts:3,keys:5,realtime:913,logs:913,failed:7,inflight:2}});
  if(p==='/call-monitor/realtime')return json({records: url.searchParams.get('failed_only')==='true'?records.filter(r=>!r.success):records,total:records.length,total_pages:1,counters:{accounts:3,keys:5,realtime:913,logs:913,failed:7,inflight:2}});
  if(p==='/call-monitor/content')return json({request:{model:'gemini-2.5-pro',messages:[{role:'user',content:'这是一条演示请求'}]},response:{content:'演示响应'}});
  if(p==='/billing/pricing')return json({currency:'CNY',default:{input_price:'3',output_price:'15',cache_price:'0.3'},models:[]});
  if(p==='/billing/dashboard') {
   const count=({today:1,'7d':7,'14d':14,'30d':30})[url.searchParams.get('range')]||1;
   const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
   const end=new Date(today+'T00:00:00Z');
   const daily=Array.from({length:count},(_,i)=>{const date=new Date(end);date.setUTCDate(date.getUTCDate()-(count-1-i));return {billing_date:date.toISOString().slice(0,10),total_tokens:1542000+i*391280,total_cost:Number((12.32+i*2.17).toFixed(2))};});
   const total=daily.reduce((n,d)=>n+d.total_tokens,0),cost=daily.reduce((n,d)=>n+d.total_cost,0);
   return json({summary:{total_tokens:total,input_tokens:Math.floor(total*.7),output_tokens:total-Math.floor(total*.7),cache_tokens:0,thought_tokens:0,total_cost:cost,input_cost:cost*.45,output_cost:cost*.55,cache_cost:0,success_count:1192*count,failed_count:3*count,unknown_usage_count:0,daily_trend:daily},accounts:{items:[rows[0],rows[3],rows[6]]},models:{items:models.map((model,i)=>({model,total_tokens:Math.round(total/(i+2)),total_cost:cost/(i+2)}))},keys:{items:keys.slice(0,3).map(k=>({api_key_id:k.id,total_tokens:k.quota_tokens_used,total_cost:k.quota_used}))}});
  }
  if(p==='/auth/env-creds-status')return json({has_env_creds:false});
  if(p.endsWith('/models')||p.includes('/quota/'))return json({items:[],models:{},results:{}});
  if(p.includes('/telemetry'))return json({mode:'off',events:[]});
  if(p.includes('/capture/status'))return json({enabled:false,files:[],total_bytes:0});
  if(method!=='GET')return json({detail:'本地设计预览：此操作需要连接实际服务。'},409);
  return json({success:true,items:[],data:{},groups:[],batches:[]});
 };
 window.connectWebSocket = function(){
  document.getElementById('connectionStatusText').textContent='演示日志 · 本地预览';
  AppState.allLogs = ['[13:03:18] [INFO] API gateway initialized','[13:03:19] [INFO] Credential pool ready: 3 accounts','[13:04:02] [INFO] POST /antigravity/v1/chat/completions 200 · 2.71s','[13:04:05] [WARNING] Rate limit reached · retry scheduled','[13:04:08] [INFO] Request completed · 1,247 tokens'];
  document.getElementById('logContent').textContent=AppState.allLogs.join('\n');
 };
 window.onload=async()=>{
  AppState.authToken='local-preview-only';
  document.getElementById('loginSection').classList.add('hidden');
  document.getElementById('mainSection').classList.remove('hidden');
  const badge=document.createElement('a');badge.className='preview-mode';badge.href=location.pathname==='/mobile'?'/':'/mobile';badge.textContent='本地预览 · 演示数据 ↗';badge.title='切换桌面 / 手机预览';document.querySelector('.app-header-context')?.after(badge);
  await fetchAndDisplayVersion();initTabSlider();await loadApiKeys();
  document.getElementById('getAntigravityAuthBtn')?.addEventListener('click',()=>showStatus('本地预览不发起实际账号授权。','info'));
  const tab=new URLSearchParams(location.search).get('tab');
  if(tab){const btn=[...document.querySelectorAll('.tabs .tab')].find(b=>b.getAttribute('onclick')?.includes(`'${tab}'`));if(btn)switchTab(tab,btn);}
 };
})();
