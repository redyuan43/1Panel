(() => {
  const element = document.querySelector('#comparisonLive');
  if (!element) return;
  const labels = { R0: '原方案基线', C0: '人物触发词对照', A4: 'LightX2V 四步', A8: 'LightX2V 八步', B8: 'OpenVDN 八步', C1: '人物 LoRA', D4: 'FastH3 VSA', A4_C0: 'A4＋人物触发词', A4_C05: 'A4＋人物 LoRA 0.5', A4_C1: 'A4＋人物 LoRA 1.0' };
  async function update() {
    try {
      const response = await fetch('./comparison-results/live.json', { cache: 'no-store' });
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (!data.available || Date.now() / 1000 - data.observed_at > 30) throw new Error();
      const running = data.cases.filter(record => record.status === 'running');
      const scheduled = data.cases.filter(record => ['scheduled', 'prepared'].includes(record.status));
      const describe = record => `${record.id} · ${labels[record.id] || record.id}${record.replica ? '（复验，原预览保留）' : ''} · ${record.lane || '通道待确认'}`;
      element.textContent = (running.length ? '视频比较正在生成：' + running.map(record => `${describe(record)}（${record.started_at ? Math.floor((Date.now() / 1000 - record.started_at) / 60) + '分钟' : '待计时'}）`).join('、') : '视频比较：当前无已确认运行的任务')
        + (scheduled.length ? '；待准入（尚未提交）：' + scheduled.map(describe).join('、') : '') + ' · 查看全部方案与视频 →';
    } catch {
      element.textContent = '视频比较：实时状态暂不可用 · 查看对比页 →';
    }
  }
  update();
  setInterval(update, 5000);
})();
