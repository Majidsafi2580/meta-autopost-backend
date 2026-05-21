import os, json, secrets, string
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import requests
from flask import Flask, request, jsonify, redirect, render_template_string
from dotenv import load_dotenv

load_dotenv()
APP_ID=os.getenv('META_APP_ID','').strip(); APP_SECRET=os.getenv('META_APP_SECRET','').strip(); BASE_URL=os.getenv('BASE_URL','').rstrip('/'); REDIRECT_URI=os.getenv('META_REDIRECT_URI','').strip()
GRAPH_VERSION=os.getenv('META_GRAPH_VERSION','v25.0'); BOT_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip(); ADMIN_CHAT_ID=os.getenv('ADMIN_CHAT_ID','').strip()
DATA_DIR=Path(os.getenv('DATA_DIR','data')); DATA_DIR.mkdir(exist_ok=True)
ACCOUNTS=DATA_DIR/'accounts.json'; OAUTH_STATES=DATA_DIR/'oauth_states.json'; BOT_STATES=DATA_DIR/'bot_states.json'; SETTINGS=DATA_DIR/'user_settings.json'; SCHEDULED=DATA_DIR/'scheduled_posts.json'; LOGS=DATA_DIR/'posts_log.json'; PROCESSED=DATA_DIR/'processed_updates.json'
SCOPES=['pages_show_list','pages_read_engagement','pages_manage_posts','business_management']
TIMEZONES={'Africa/Algiers':'🇩🇿 المغرب العربي','Africa/Casablanca':'🇲🇦 المغرب','Africa/Tunis':'🇹🇳 تونس','Africa/Cairo':'🇪🇬 مصر','Asia/Riyadh':'🇸🇦 السعودية','Asia/Dubai':'🇦🇪 الإمارات','America/New_York':'🇺🇸 شرق أمريكا','America/Chicago':'🇺🇸 وسط أمريكا','America/Los_Angeles':'🇺🇸 غرب أمريكا'}
app=Flask(__name__)

def load_json(p,d):
    if not p.exists(): return d
    try: return json.loads(p.read_text(encoding='utf-8'))
    except Exception: return d

def save_json(p,d): p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
def now_iso(): return datetime.now(timezone.utc).isoformat()
def sid(n=8): return ''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(n))
def clean(e):
    try:
        d=json.loads(str(e)); return d.get('error',{}).get('message',str(e))
    except Exception: return str(e)

def graph_get(path,params):
    r=requests.get('https://graph.facebook.com/'+GRAPH_VERSION+'/'+path.lstrip('/'),params=params,timeout=60); d=r.json()
    if r.status_code>=400: raise RuntimeError(json.dumps(d,ensure_ascii=False))
    return d

def graph_post(path,data=None,params=None):
    r=requests.post('https://graph.facebook.com/'+GRAPH_VERSION+'/'+path.lstrip('/'),data=data or {},params=params or {},timeout=240); d=r.json()
    if r.status_code>=400: raise RuntimeError(json.dumps(d,ensure_ascii=False))
    return d

def tg(method,payload):
    if not BOT_TOKEN: return None
    try: return requests.post('https://api.telegram.org/bot'+BOT_TOKEN+'/'+method,json=payload,timeout=30).json()
    except Exception: return None

def main_keyboard():
    return {'keyboard':[[{'text':'🏠 الرئيسية'},{'text':'📊 لوحة التحكم'}],[{'text':'➕ ربط Meta'},{'text':'📄 حساباتي'}],[{'text':'🎯 اختيار الصفحات'},{'text':'🌐 اختيار المنصة'}],[{'text':'🌍 اختيار التوقيت'},{'text':'🔄 تحديث الحسابات'}],[{'text':'📝 نشر نص'},{'text':'🖼 نشر صورة'}],[{'text':'🎬 نشر فيديو'},{'text':'⏰ جدولة منشور'}],[{'text':'📋 المجدولات'},{'text':'🗑 حذف مجدول'}],[{'text':'❌ إلغاء'}]],'resize_keyboard':True}

def send(chat_id,text,markup=None):
    payload={'chat_id':chat_id,'text':text,'disable_web_page_preview':True,'reply_markup': markup if markup is not None else main_keyboard()}
    return tg('sendMessage',payload)

def file_url(file_id):
    info=tg('getFile',{'file_id':file_id})
    if not info or not info.get('ok'): raise RuntimeError('فشل جلب الملف من Telegram')
    return 'https://api.telegram.org/file/bot'+BOT_TOKEN+'/'+info['result']['file_path']

def allowed(chat_id): return (not ADMIN_CHAT_ID) or str(chat_id)==str(ADMIN_CHAT_ID)
def get_state(chat_id): return load_json(BOT_STATES,{}).get(str(chat_id))
def set_state(chat_id,state):
    s=load_json(BOT_STATES,{}); s[str(chat_id)]=state; save_json(BOT_STATES,s)
def clear_state(chat_id):
    s=load_json(BOT_STATES,{}); s.pop(str(chat_id),None); save_json(BOT_STATES,s)
def get_settings(chat_id):
    s=load_json(SETTINGS,{}); k=str(chat_id)
    if k not in s: s[k]={'selected_pages':[],'platforms':['facebook'],'timezone':'Africa/Algiers'}; save_json(SETTINGS,s)
    return s[k]
def update_settings(chat_id,upd):
    s=load_json(SETTINGS,{}); k=str(chat_id); cur=get_settings(chat_id); cur.update(upd); s[k]=cur; save_json(SETTINGS,s); return cur

def get_account(chat_id):
    accs=load_json(ACCOUNTS,[]); k=str(chat_id)
    for a in reversed(accs):
        if str(a.get('telegram_id'))==k: return a
    return None

def save_account(chat_id,meta_user,user_token,pages):
    accs=[a for a in load_json(ACCOUNTS,[]) if str(a.get('telegram_id'))!=str(chat_id)]
    accs.append({'telegram_id':str(chat_id),'meta_user':meta_user,'user_access_token':user_token,'pages':pages,'connected_at':now_iso()}); save_json(ACCOUNTS,accs)
    if pages: update_settings(chat_id,{'selected_pages':[p['id'] for p in pages],'platforms':['facebook']})

def get_pages(chat_id):
    a=get_account(chat_id); return a.get('pages',[]) if a else []

def log(entry):
    l=load_json(LOGS,[]); entry['logged_at']=now_iso(); l.append(entry); save_json(LOGS,l[-1000:])

def publish_fb(page,typ,text='',url=None):
    pid=page['id']; tok=page['access_token']
    if typ=='text': return graph_post('/'+pid+'/feed',data={'message':text,'access_token':tok})
    if typ=='photo': return graph_post('/'+pid+'/photos',data={'url':url,'caption':text or '', 'access_token':tok})
    if typ=='video': return graph_post('/'+pid+'/videos',data={'file_url':url,'description':text or '', 'access_token':tok})
    raise RuntimeError('نوع غير مدعوم')

def publish_ig(page,typ,text='',url=None):
    ig=page.get('instagram_business_account')
    if not ig: raise RuntimeError('Instagram غير مربوط بهذه الصفحة')
    tok=page['access_token']; igid=ig['id']
    if typ=='text': raise RuntimeError('Instagram لا يدعم نص فقط')
    data={'caption':text or '','access_token':tok}
    if typ=='photo': data['image_url']=url
    elif typ=='video': data.update({'media_type':'REELS','video_url':url})
    c=graph_post('/'+igid+'/media',data=data)
    return graph_post('/'+igid+'/media_publish',data={'creation_id':c['id'],'access_token':tok})

def publish_content(chat_id,typ,text='',url=None,page_ids=None,platforms=None,notify=True):
    pages=get_pages(chat_id)
    if not pages: raise RuntimeError('لا توجد صفحات. أعد ربط Meta')
    st=get_settings(chat_id); page_ids=page_ids or st.get('selected_pages') or [p['id'] for p in pages]; platforms=platforms or st.get('platforms') or ['facebook']
    mp={str(p['id']):p for p in pages}; chosen=[mp[str(x)] for x in page_ids if str(x) in mp] or pages
    total=len(chosen)*len(platforms); step=0; results=[]
    if notify: send(chat_id,'🚀 بدأ النشر...\n📌 عدد الوجهات: '+str(total))
    for page in chosen:
        for platform in platforms:
            step+=1; name=page.get('name','Page'); item={'platform':platform,'page_id':page.get('id'),'page_name':name,'success':False}
            try:
                if notify: send(chat_id,'⏳ جاري النشر في الصفحة '+str(step)+'/'+str(total)+'\n📄 '+name+'\n🌐 '+platform)
                res=publish_fb(page,typ,text,url) if platform=='facebook' else publish_ig(page,typ,text,url)
                item.update({'success':True,'post_id':res.get('id') or res.get('post_id'),'raw':res})
                if notify: send(chat_id,'✅ تم النشر في الصفحة '+str(step)+'/'+str(total)+'\n📄 '+name+'\n🆔 Post ID: '+str(item.get('post_id')))
            except Exception as e:
                item['error']=clean(e)
                if notify: send(chat_id,'❌ فشل النشر في الصفحة '+str(step)+'/'+str(total)+'\n📄 '+name+'\nالسبب: '+item['error'])
            results.append(item); log({'telegram_id':str(chat_id),'type':typ,'platform':platform,'page_id':page.get('id'),'page_name':name,'success':item['success'],'post_id':item.get('post_id'),'error':item.get('error')})
    if notify:
        ok=len([r for r in results if r.get('success')]); fail=len(results)-ok
        send(chat_id,'🏁 انتهت عملية النشر\n✅ نجح: '+str(ok)+'\n❌ فشل: '+str(fail))
    return results

def dashboard(chat_id):
    a=get_account(chat_id); st=get_settings(chat_id); schedules=load_json(SCHEDULED,[]); pending=len([p for p in schedules if str(p.get('telegram_id'))==str(chat_id) and p.get('status')=='pending'])
    return '\n'.join(['📊 AutoPost Pro','', 'Meta: '+('✅ مربوط' if a else '❌ غير مربوط'),'الصفحات: '+str(len(a.get('pages',[])) if a else 0),'الصفحات المختارة: '+str(len(st.get('selected_pages',[]))),'المنصات: '+', '.join(st.get('platforms',['facebook'])),'التوقيت: '+st.get('timezone','Africa/Algiers'),'المجدولات pending: '+str(pending)])

def accounts_text(chat_id):
    a=get_account(chat_id)
    if not a: return '❌ لا يوجد حساب Meta مربوط'
    st=get_settings(chat_id); sel={str(x) for x in st.get('selected_pages',[])}; lines=['📄 الحسابات والصفحات','','Meta: '+a.get('meta_user',{}).get('name','Unknown'),'عدد الصفحات: '+str(len(a.get('pages',[]))),'المنصات: '+', '.join(st.get('platforms',['facebook'])),'التوقيت: '+st.get('timezone','Africa/Algiers'),'','الصفحات:']
    for i,p in enumerate(a.get('pages',[]),1):
        mark='✅' if str(p.get('id')) in sel else '⬜'; ig=p.get('instagram_business_account'); igtext='IG: @'+ig.get('username') if ig else 'IG: غير مربوط'
        lines.append(mark+' '+str(i)+'. '+p.get('name','Page')+' | token: '+('✅' if p.get('access_token') else '❌')+' | '+igtext)
    return '\n'.join(lines)

@app.get('/')
def home(): return jsonify({'ok':True,'name':'AutoPost Pro Clean','missing_env':[k for k,v in {'META_APP_ID':APP_ID,'META_APP_SECRET':APP_SECRET,'BASE_URL':BASE_URL,'META_REDIRECT_URI':REDIRECT_URI}.items() if not v],'telegram_ready':bool(BOT_TOKEN)})
@app.get('/privacy')
def privacy(): return render_template_string('<h1>Privacy Policy</h1><p>This app publishes selected content to authorized Meta pages.</p>')
@app.get('/terms')
def terms(): return render_template_string('<h1>Terms</h1><p>Users are responsible for content and Meta permissions.</p>')
@app.route('/data-deletion',methods=['GET','POST'])
def deletion():
    if request.method=='GET': return render_template_string('<h1>Data Deletion</h1><p>Contact admin.</p>')
    code=secrets.token_hex(8); return jsonify({'url':BASE_URL+'/data-deletion/status/'+code,'confirmation_code':code})
@app.get('/data-deletion/status/<code>')
def del_status(code): return jsonify({'status':'received','confirmation_code':code})
@app.get('/auth/meta/start')
def meta_start():
    tid=request.args.get('telegram_id','admin'); state=secrets.token_urlsafe(24); states=load_json(OAUTH_STATES,{})
    states[state]={'telegram_id':str(tid),'created_at':now_iso()}; save_json(OAUTH_STATES,states)
    params={'client_id':APP_ID,'redirect_uri':REDIRECT_URI,'state':state,'scope':','.join(SCOPES),'response_type':'code'}
    return redirect('https://www.facebook.com/'+GRAPH_VERSION+'/dialog/oauth?'+urlencode(params))
@app.get('/auth/meta/callback')
def meta_callback():
    code=request.args.get('code'); state=request.args.get('state')
    states=load_json(OAUTH_STATES,{}); saved=states.pop(state,None); save_json(OAUTH_STATES,states)
    if not code or not saved: return jsonify({'ok':False,'error':'Invalid or expired state'}),400
    try:
        short=graph_get('/oauth/access_token',{'client_id':APP_ID,'client_secret':APP_SECRET,'redirect_uri':REDIRECT_URI,'code':code})['access_token']
        long=graph_get('/oauth/access_token',{'grant_type':'fb_exchange_token','client_id':APP_ID,'client_secret':APP_SECRET,'fb_exchange_token':short})['access_token']
        me=graph_get('/me',{'access_token':long,'fields':'id,name'})
        pages=graph_get('/me/accounts',{'access_token':long,'fields':'id,name,access_token,instagram_business_account{id,username,name,profile_picture_url}'}).get('data',[])
        save_account(saved['telegram_id'],me,long,pages)
        if str(saved['telegram_id']).isdigit(): send(saved['telegram_id'],'✅ تم ربط Meta بنجاح\nالحساب: '+me.get('name','Unknown')+'\nعدد الصفحات: '+str(len(pages)))
        return render_template_string('<h1>✅ Account connected</h1><p>Pages found: {{count}}</p>',count=len(pages))
    except Exception as e: return jsonify({'ok':False,'error':clean(e)}),500
@app.get('/accounts')
def accounts():
    out=[]
    for a in load_json(ACCOUNTS,[]):
        safe={'telegram_id':a.get('telegram_id'),'meta_user':a.get('meta_user'),'connected_at':a.get('connected_at'),'pages':[]}
        for p in a.get('pages',[]): safe['pages'].append({'id':p.get('id'),'name':p.get('name'),'has_page_token':bool(p.get('access_token')),'instagram_business_account':p.get('instagram_business_account')})
        out.append(safe)
    return jsonify({'ok':True,'accounts':out})
@app.get('/telegram/set-webhook')
def set_webhook():
    url=BASE_URL+'/telegram/webhook'; return jsonify({'ok':True,'webhook_url':url,'telegram':tg('setWebhook',{'url':url})})
@app.get('/telegram/delete-webhook')
def delete_webhook(): return jsonify({'ok':True,'telegram':tg('deleteWebhook',{})})

def parse_time(txt,tz):
    z=ZoneInfo(tz); local=datetime.strptime(txt.strip(),'%Y-%m-%d %H:%M').replace(tzinfo=z); return local.isoformat(), local.astimezone(timezone.utc).isoformat()
@app.get('/cron/publish-scheduled')
def cron():
    posts=load_json(SCHEDULED,[]); now=datetime.now(timezone.utc); done=[]; changed=False
    for p in posts:
        if p.get('status')!='pending': continue
        try:
            t=datetime.fromisoformat(p['scheduled_utc']); t=t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            if t>now: continue
            res=publish_content(p['telegram_id'],p['type'],text=p.get('text',''),url=p.get('file_url'),page_ids=p.get('selected_pages'),platforms=p.get('platforms'),notify=True)
            ok=len([x for x in res if x.get('success')]); p['results']=res; p['status']='published' if ok==len(res) and res else ('partial' if ok else 'failed'); p['published_at']=now_iso(); done.append({'id':p.get('id'),'status':p.get('status')}); changed=True
        except Exception as e:
            p['status']='failed'; p['error']=clean(e); p['failed_at']=now_iso(); done.append({'id':p.get('id'),'status':'failed','error':p['error']}); changed=True
    if changed: save_json(SCHEDULED,posts)
    return jsonify({'ok':True,'processed':done})

def extract(message,typ,text):
    if typ=='text':
        if not text: raise RuntimeError('اكتب نص المنشور')
        return text,None
    if typ=='photo':
        photos=message.get('photo') or []
        if not photos: raise RuntimeError('ابعث صورة')
        return text or '', file_url(photos[-1]['file_id'])
    if typ=='video':
        vid=message.get('video')
        if not vid: raise RuntimeError('ابعث فيديو')
        return text or '', file_url(vid['file_id'])
    raise RuntimeError('نوع غير مدعوم')

def handle_state(chat_id,message,text,state):
    action=state.get('action')
    if action in ['await_text_now','await_photo_now','await_video_now']:
        typ=action.replace('await_','').replace('_now',''); body,url=extract(message,typ,text); clear_state(chat_id); publish_content(chat_id,typ,text=body,url=url,notify=True); return True
    if action.startswith('await_schedule_content_'):
        typ=state['type']; body,url=extract(message,typ,text); state.update({'action':'await_schedule_time','text':body,'file_url':url}); set_state(chat_id,state); send(chat_id,'🕒 اكتب وقت النشر:\nYYYY-MM-DD HH:MM\n\nالتوقيت الحالي: '+get_settings(chat_id).get('timezone','Africa/Algiers')); return True
    if action=='await_schedule_time':
        st=get_settings(chat_id); local,utc=parse_time(text,st.get('timezone','Africa/Algiers')); pages=get_pages(chat_id); selected=st.get('selected_pages') or [p['id'] for p in pages]
        post={'id':sid(),'telegram_id':str(chat_id),'type':state['type'],'platforms':st.get('platforms',['facebook']),'selected_pages':selected,'text':state.get('text',''),'file_url':state.get('file_url'),'timezone':st.get('timezone','Africa/Algiers'),'scheduled_local':text,'scheduled_local_iso':local,'scheduled_utc':utc,'status':'pending','created_at':now_iso(),'results':[]}
        arr=load_json(SCHEDULED,[]); arr.append(post); save_json(SCHEDULED,arr); clear_state(chat_id); send(chat_id,'✅ تمت الجدولة\nID: '+post['id']+'\nالوقت: '+text); return True
    if action=='await_pages':
        pages=get_pages(chat_id); selected=[]
        if text.lower().strip()=='all': selected=[p['id'] for p in pages]
        else:
            for part in text.replace('،',',').split(','):
                part=part.strip()
                if part.isdigit() and 0<=int(part)-1<len(pages): selected.append(pages[int(part)-1]['id'])
        if not selected: raise RuntimeError('اختيار غير صحيح. اكتب all أو 1,2')
        state.update({'action':'await_platforms','selected_pages':selected}); set_state(chat_id,state); send(chat_id,'🌐 اختر المنصة:\nfacebook\ninstagram\nboth'); return True
    if action=='await_platforms':
        v=text.lower().strip(); platforms={'facebook':['facebook'],'instagram':['instagram'],'both':['facebook','instagram']}.get(v)
        if not platforms: send(chat_id,'اكتب facebook أو instagram أو both'); return True
        update_settings(chat_id,{'selected_pages':state.get('selected_pages',[]),'platforms':platforms}); clear_state(chat_id); send(chat_id,'✅ تم حفظ وجهة النشر\n\n'+accounts_text(chat_id)); return True
    if action=='await_timezone':
        if text not in TIMEZONES: send(chat_id,'توقيت غير صحيح. مثال: Africa/Algiers'); return True
        update_settings(chat_id,{'timezone':text}); clear_state(chat_id); send(chat_id,'✅ تم اختيار التوقيت: '+text); return True
    if action=='await_delete_schedule':
        arr=load_json(SCHEDULED,[]); found=False
        for p in arr:
            if str(p.get('telegram_id'))==str(chat_id) and p.get('id')==text:
                found=True
                if p.get('status')=='pending': p['status']='cancelled'; p['cancelled_at']=now_iso(); save_json(SCHEDULED,arr); send(chat_id,'✅ تم إلغاء المنشور المجدول: '+text)
                else: send(chat_id,'لا يمكن إلغاء هذا المنشور. الحالة: '+str(p.get('status')))
        if not found: send(chat_id,'لم أجد منشور بهذا ID')
        clear_state(chat_id); return True
    return False

@app.post('/telegram/webhook')
def webhook():
    chat_id=None
    try:
        upd=request.get_json(silent=True) or {}; uid=upd.get('update_id')
        if uid is not None:
            proc=load_json(PROCESSED,[])
            if uid in proc: return jsonify({'ok':True,'duplicate':True})
            proc.append(uid); save_json(PROCESSED,proc[-500:])
        msg=upd.get('message') or upd.get('edited_message') or {}; chat=msg.get('chat') or {}; chat_id=chat.get('id'); text=(msg.get('text') or msg.get('caption') or '').strip()
        if not chat_id: return jsonify({'ok':True})
        if not allowed(chat_id): send(chat_id,'❌ هذا البوت خاص.',None); return jsonify({'ok':True})
        state=get_state(chat_id)
        if text in ['/start','start','🏠 الرئيسية']: clear_state(chat_id); send(chat_id,'🏠 AutoPost Pro\n\nاختر من القائمة:')
        elif text=='❌ إلغاء': clear_state(chat_id); send(chat_id,'تم الإلغاء')
        elif text=='📊 لوحة التحكم': send(chat_id,dashboard(chat_id))
        elif text=='➕ ربط Meta': send(chat_id,'اضغط الرابط واربط حساب Meta:\n'+BASE_URL+'/auth/meta/start?telegram_id='+str(chat_id))
        elif text=='📄 حساباتي': send(chat_id,accounts_text(chat_id))
        elif text=='🔄 تحديث الحسابات':
            a=get_account(chat_id)
            if not a: send(chat_id,'❌ لا يوجد حساب مربوط')
            else:
                pages=graph_get('/me/accounts',{'access_token':a['user_access_token'],'fields':'id,name,access_token,instagram_business_account{id,username,name,profile_picture_url}'}).get('data',[]); save_account(chat_id,a.get('meta_user',{}),a['user_access_token'],pages); send(chat_id,'✅ تم تحديث الحسابات\nعدد الصفحات: '+str(len(pages)))
        elif text=='🎯 اختيار الصفحات':
            pages=get_pages(chat_id); lines=['🎯 اختر الصفحات:','اكتب all أو أرقام مثل 1,2','']
            for i,p in enumerate(pages,1): lines.append(str(i)+'. '+p.get('name','Page'))
            set_state(chat_id,{'action':'await_pages'}); send(chat_id,'\n'.join(lines))
        elif text=='🌐 اختيار المنصة': set_state(chat_id,{'action':'await_platforms','selected_pages':get_settings(chat_id).get('selected_pages',[])}); send(chat_id,'🌐 اكتب المنصة:\nfacebook\ninstagram\nboth')
        elif text=='🌍 اختيار التوقيت': set_state(chat_id,{'action':'await_timezone'}); send(chat_id,'🌍 اكتب التوقيت مثل: Africa/Algiers\n\n'+'\n'.join([v+': '+k for k,v in TIMEZONES.items()]))
        elif text=='📝 نشر نص': set_state(chat_id,{'action':'await_text_now'}); send(chat_id,'📝 اكتب نص المنشور الآن')
        elif text=='🖼 نشر صورة': set_state(chat_id,{'action':'await_photo_now'}); send(chat_id,'🖼 ابعث الصورة ومعها caption اختياري')
        elif text=='🎬 نشر فيديو': set_state(chat_id,{'action':'await_video_now'}); send(chat_id,'🎬 ابعث الفيديو ومعاه caption اختياري')
        elif text=='⏰ جدولة منشور': set_state(chat_id,{'action':'await_schedule_type'}); send(chat_id,'⏰ اختر نوع المنشور:',{'keyboard':[[{'text':'📝 جدولة نص'},{'text':'🖼 جدولة صورة'}],[{'text':'🎬 جدولة فيديو'}],[{'text':'❌ إلغاء'}]],'resize_keyboard':True})
        elif text in ['📝 جدولة نص','نص'] and state and state.get('action')=='await_schedule_type': set_state(chat_id,{'action':'await_schedule_content_text','type':'text'}); send(chat_id,'أرسل نص المنشور المجدول')
        elif text in ['🖼 جدولة صورة','صورة'] and state and state.get('action')=='await_schedule_type': set_state(chat_id,{'action':'await_schedule_content_photo','type':'photo'}); send(chat_id,'أرسل الصورة مع caption اختياري للجدولة')
        elif text in ['🎬 جدولة فيديو','فيديو'] and state and state.get('action')=='await_schedule_type': set_state(chat_id,{'action':'await_schedule_content_video','type':'video'}); send(chat_id,'أرسل الفيديو مع caption اختياري للجدولة')
        elif text=='📋 المجدولات':
            arr=[p for p in load_json(SCHEDULED,[]) if str(p.get('telegram_id'))==str(chat_id)]
            if not arr: send(chat_id,'لا توجد منشورات مجدولة')
            else: send(chat_id,'📋 آخر 10 مجدولات:\n'+'\n'.join(['ID: '+p.get('id','-')+' | '+p.get('type','-')+' | '+p.get('scheduled_local','-')+' | '+p.get('status','-') for p in arr[-10:]]))
        elif text=='🗑 حذف مجدول': set_state(chat_id,{'action':'await_delete_schedule'}); send(chat_id,'أرسل ID تاع المنشور المجدول')
        elif state:
            try:
                if not handle_state(chat_id,msg,text,state): send(chat_id,'لم أفهم الطلب')
            except Exception as e: send(chat_id,'❌ خطأ:\n'+clean(e))
        else: send(chat_id,'اختر من القائمة')
    except Exception as e:
        if chat_id: send(chat_id,'❌ خطأ عام:\n'+clean(e))
    return jsonify({'ok':True})

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')),debug=True)
