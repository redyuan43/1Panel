(() => {
  const element = document.querySelector('#comparisonLive');
  if (!element) return;
  const labels = { R0: '原方案基线', C0: '人物触发词对照', A4: 'LightX2V 四步', A8: 'LightX2V 八步', B8: 'OpenVDN 八步', C1: '人物 LoRA', D4: 'FastH3 VSA', A4_C0: 'A4＋人物触发词', A4_C05: 'A4＋人物 LoRA 0.5', A4_C1: 'A4＋人物 LoRA 1.0' };
  async function update() {
    try {
      const response = await fetch('./comparison-results/live.json', { cache: 'no-store' });
      if (!response.ok) throw new Error();
      const data = await response.json();
      const age = Date.now() / 1000 - data.observed_at;
      // observed_at 由产出侧服务时钟写入，age 用浏览器时钟计算，允许 5 秒以内的跨机时钟偏差。
      if (!data.available || !Number.isFinite(age) || age < -5 || age > 30 || !Array.isArray(data.cases)) throw new Error();
      const running = data.cases.filter(record => record.status === 'running');
      const scheduled = data.cases.filter(record => ['scheduled', 'prepared'].includes(record.status));
      const describe = record => `${record.id} · ${labels[record.id] || record.id}${record.replica ? '（复验，原预览保留）' : ''} · ${record.lane || '通道待确认'}`;
      element.textContent = (running.length ? '配方比较实验正在生成：' + running.map(record => `${describe(record)}（${record.started_at ? Math.max(0, Math.floor((Date.now() / 1000 - record.started_at) / 60)) + '分钟' : '待计时'}）`).join('、') : '配方比较实验：暂无运行中的比较实验')
        + (scheduled.length ? '；待准入（尚未提交）：' + scheduled.map(describe).join('、') : '')
        + '。不含工作室生成任务；工作室任务请看下方流程及历史任务 · 查看比较样片 →';
    } catch {
      element.textContent = '配方比较实验：状态暂不可核实（不代表工作室无任务） · 查看对比页 →';
    }
  }
  update();
  setInterval(update, 5000);
})();
