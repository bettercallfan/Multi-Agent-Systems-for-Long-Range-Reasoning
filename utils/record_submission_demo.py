"""Record actual read-only dashboard interactions, explicitly labeled replay."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler

from playwright.sync_api import sync_playwright


def export_video(path, out, timeline, errors):
    if errors:
        raise RuntimeError('Browser errors: '+repr(errors))
    # Some Conda builds lack libx264; prefer the verified system FFmpeg.
    binary=os.getenv('FFMPEG_BIN') or ('/usr/bin/ffmpeg' if Path('/usr/bin/ffmpeg').is_file() else 'ffmpeg')
    env=dict(os.environ)
    if binary=='/usr/bin/ffmpeg':env['LD_LIBRARY_PATH']='/usr/lib/x86_64-linux-gnu'
    subprocess.run([binary,'-hide_banner','-loglevel','error','-y','-i',str(path),'-vf','fps=25',
        '-c:v','libx264','-preset','fast','-crf','22','-pix_fmt','yuv420p','-an','-movflags','+faststart',str(out/'系统完整演示.mp4')],check=True,env=env)
    write_video_metadata(out, timeline, errors)
    print('Video completed',out/'系统完整演示.mp4',flush=True)


def write_video_metadata(out, timeline, errors):
    (out/'视频章节与来源.json').write_text(json.dumps({'mode':'historical_real_run_replay','browser_errors':errors,'timeline':timeline,'runs':list(RUN_IDS)},ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'视频说明.md').write_text('# 系统完整演示\n\n本片直接录制随包演示台的真实浏览器操作，使用归档运行回放。视频带文字导览、无配音；等待时长压缩，章节之间有跳转，不是实时模型执行。\n\n'+ '\n'.join(f"- {x['seconds']:.1f}s：{x['title']}。{x['text']}" for x in timeline)+'\n',encoding='utf-8')


def record(source: Path, out: Path, work: Path):
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    url=f"http://127.0.0.1:{port}"
    log=(work/'server.log').open('w')
    proc=subprocess.Popen([sys.executable,'-u','utils/dashboard_server.py','--host','127.0.0.1','--port',str(port)],cwd=source,stdout=log,stderr=subprocess.STDOUT)
    client=build_opener(ProxyHandler({})); timeline=[]; errors=[]
    try:
        for _ in range(100):
            try:
                with client.open(url+'/api/presentation',timeout=2) as r:
                    catalog=json.load(r)
                break
            except OSError:time.sleep(.2)
        else:raise RuntimeError('Read-only demo server did not start')
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            context=browser.new_context(viewport={'width':1920,'height':1080},device_scale_factor=1,
                record_video_dir=str(work/'video'),record_video_size={'width':1920,'height':1080})
            pg=context.new_page();pg.on('pageerror',lambda e:errors.append(str(e)))
            pg.goto(url+'/demo');pg.wait_for_function("document.querySelector('#run-select').options.length >= 7")
            pg.locator('#recording-layout').click()
            start=time.monotonic()
            def caption(title,copy):
                timeline.append({'seconds':round(time.monotonic()-start,3),'title':title,'text':copy})
                pg.evaluate('''([title,copy])=>{let e=document.getElementById('record-caption');if(!e){e=document.createElement('div');e.id='record-caption';e.style.cssText='position:fixed;bottom:0;left:0;right:0;z-index:100000;background:rgba(18,33,53,.96);color:white;padding:13px 36px;font-family:"Noto Sans CJK SC",sans-serif;box-shadow:0 -2px 20px #0002;pointer-events:none';document.body.append(e)}e.replaceChildren();let h=document.createElement('div');h.style.cssText='font-size:22px;font-weight:700';h.textContent=title;let t=document.createElement('div');t.style.cssText='font-size:18px;line-height:1.6;margin-top:5px;color:#d8e6f5';t.textContent=copy;e.append(h,t)}''',[title,copy])
                print(title,flush=True)
            def pause(seconds):pg.wait_for_timeout(seconds*1000)
            def select(ident):
                pg.locator('#run-select').select_option(ident)
                pg.wait_for_function('(id)=>document.querySelector("#source-run").textContent===id',arg=ident)
                pg.evaluate('window.scrollTo(0,0)')
            def seek(index):
                pg.locator('#scrubber').evaluate('(e,n)=>{e.value=n;e.dispatchEvent(new Event("input",{bubbles:true}))}',index)
            def close():
                if pg.locator('#detail-dialog').evaluate('(e)=>e.open'):pg.locator('#close-dialog').click()
            select('20260910_163108')
            caption('XH-202631｜多智能体动态协作与自治交付系统','西安交通大学 · 指导老师：刘恒嘉、曾菊香 · 申报人：陈若凡。本片为历史真实运行回放，不是本轮实时模型调用。');pause(12)
            caption('01｜高层意图与可验收契约','读取真实任务输入、约束及预期交付；初始状态尚未完成，验收不会提前显示 PASS。');pause(12)
            pg.locator('#source-button').click();pause(6);close()
            pg.locator('[data-chapter="1"]').click()
            caption('02｜动态任务协作图','节点职责和连线来自保存的任务拓扑，运行状态沿真实事件顺序投影；这不是逐时刻图快照。');pause(15)
            with client.open(url+'/api/runs/20260910_163108') as r:detail=json.load(r)
            # Use the same actual event sequence consumed by the production UI.
            events=pg.evaluate('window.DemoModel.eventsFor',detail)
            types=[('node_failure','节点失效','查看失败检测、恢复决策和实际重试；恢复由主调度器完成。'),
                   ('requirement_change','需求变更','更新待执行契约、递增图版本，已经完成的节点保持不变。'),
                   ('data_anomaly','数据异常','输入副本被真实扰动；SHA256 检测差异，隔离异常、恢复原件并重新验证。')]
            for typ,label,copy in types:
                begin=next(i for i,e in enumerate(events) if e['type']=='runtime_injection_triggered' and e.get('payload',{}).get('type')==typ)
                ident=events[begin]['payload'].get('injection_id')
                ending=next(i for i,e in enumerate(events[begin:],begin) if e['type']=='runtime_injection_recovered' and (e.get('payload',{}).get('injection_id')==ident or e.get('payload',{}).get('type')==typ))
                seek(begin);caption('03｜'+label,copy);pause(7)
                for n in range(begin+1,ending+1):seek(n);pause(max(.15,10/max(1,ending-begin)))
                pg.locator('#injections').scroll_into_view_if_needed();pause(7)
                # Production card's evidence button shows saved evidence, not invented text.
                cards=pg.locator('.injection-card').filter(has_text=label)
                button=cards.locator('button').last
                if button.count():button.click();pause(7);close()
                pg.evaluate('window.scrollTo(0,0)');pause(3)
            pg.locator('[data-chapter="5"]').click();pg.locator('.delivery').scroll_into_view_if_needed()
            caption('04｜最终交付与九项独立审计','只有业务验证、需求验收、证据闭合和报告等条件全部满足，最终状态才允许成功。');pause(12)
            pg.locator('#delivery-button').click();pause(10);close()
            for ident,label,text in [
                ('20260908_124006','数据建模 / 装箱优化','300 件货物完整覆盖，5 辆车，总成本 3500；结果来自真实业务验证产物。'),
                ('20260908_125323','跨文档 / 差旅报销','PDF、DOCX、XLSX 联合核验，费用总额 CNY 1,485；原始报告和审计可下载。'),
                ('20260908_173502','城市 / 多模态数据分析','遥感元数据、法规、匿名通联与 PCAP 包头统计；不进行个人身份关联或因果推断。'),
                ('20260909_urban_business_1000','城市业务 / 千步连续性','1,000 WorkUnit，10 次阶段模型调用；第 500 步恢复，40 个 checkpoint，重复执行 0。'),
                ('20260911_mathorcup_business_1000','装箱业务 / 千步候选搜索','1,000 个确定性候选评估，不等于 1,000 次大模型调用；结果由领域验证器复算。'),
                ('20260914_161813','补充证据 / HTTP 运行中注入','归档运行通过真实 HTTP 接口提交三类注入；主运行审计 9/9，注入审计 10/10。')]:
                select(ident)
                caption('05｜'+label,text)
                if pg.locator('[data-chapter="5"]').is_enabled():
                    pg.locator('[data-chapter="5"]').click();pause(9)
                    pg.locator('.delivery').scroll_into_view_if_needed();pause(7)
                else:
                    # Specialized thousand-step tools do not emit the generic
                    # run_finished event. Show their actual validation artifact;
                    # never manufacture that event or a replay completion gate.
                    path = ('artifacts/long_horizon_validation.json' if 'urban' in ident
                            else 'artifacts/long_horizon_search_validation.json')
                    pg.evaluate('(path)=>artifact(path)',path)
                    pause(16);close()
            caption('06｜复核路径与能力边界','提交包包含完整报告、源码、真实证据和 SHA256。端边云为逻辑后端；容器启动待实测；历史回放不能替代现场真实执行。');pause(13)
            pg.screenshot(path=str(out/'演示截图.png'),full_page=False)
            video=pg.video
            context.close();path=video.path();browser.close()
        export_video(path,out,timeline,errors)
    finally:
        (work/'recording-progress.json').write_text(json.dumps(timeline,ensure_ascii=False,indent=2),encoding='utf-8')
        (work/'browser-errors.json').write_text(json.dumps(errors,ensure_ascii=False),encoding='utf-8')
        proc.terminate()
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        log.close()


RUN_IDS=['20260910_163108','20260908_124006','20260908_125323','20260908_173502','20260909_urban_business_1000','20260911_mathorcup_business_1000','20260914_161813']
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--source',required=True);ap.add_argument('--output',required=True);ap.add_argument('--work',required=True)
    ap.add_argument('--recorded-video',help='Finish encoding an already completed browser recording')
    a=ap.parse_args()
    if a.recorded_video:
        work=Path(a.work)
        errors=json.loads((work/'browser-errors.json').read_text())
        export_video(Path(a.recorded_video),Path(a.output),json.loads((work/'recording-progress.json').read_text()),errors)
    else:
        record(Path(a.source).resolve(),Path(a.output).resolve(),Path(a.work).resolve())
