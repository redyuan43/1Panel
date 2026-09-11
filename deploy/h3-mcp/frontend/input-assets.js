"use strict";

(() => {
  const modes = {t2v: "纯文本生成", i2v: "首帧生成", l2v: "尾帧生成", fl2v: "首尾帧生成", reference: "参考素材生成", hybrid: "Hybrid 混合输入"};
  const roles = {first_frame: "首帧", last_frame: "尾帧", reference_image: "参考图", reference_video: "参考视频", reference_audio: "参考音频"};
  let currentKey = "";
  let container;

  function element(tag, text, className) {
    const result = document.createElement(tag);
    if (text) result.textContent = text;
    if (className) result.className = className;
    return result;
  }

  function render(project) {
    if (!project) return;
    const assets = project.input_assets || Object.entries(project.assets || {}).map(([kind, asset]) => ({kind, ...asset,
      preview_url: `/api/projects/${project.id}/input-assets/${kind}`}));
    const key = JSON.stringify([project.id, project.input_sha256, project.execution_profile, assets, project.connector_generation_enabled]);
    if (key === currentKey && container?.isConnected) return;
    currentKey = key;
    if (!container?.isConnected) {
      container = element("section", "", "input-material-panel");
      container.setAttribute("aria-label", "本次任务的输入素材");
      const anchor = document.getElementById("stageMetrics");
      if (!anchor) return;
      anchor.before(container);
    }
    container.replaceChildren();
    container.append(element("h3", "输入素材 · " + (modes[project.mode] || "模式未知")));
    const profile = project.execution_profile;
    const configuration = profile ? `${profile.family} 完整模型 · ${profile.steps} 步 · ${profile.profile_id}` : project.recipe_id ? `文生配方 ${project.recipe_id}` : "历史执行配置，以原任务记录为准";
    container.append(element("p", configuration));
    if (!assets.length) container.append(element("p", "未绑定图片、视频或音频。这是纯文本输入，不会自动使用聊天中的照片。", "input-material-warning"));
    const grid = element("div", "", "input-material-grid");
    for (const asset of assets) {
      const card = element("article", "", "input-material-card");
      card.append(element("strong", roles[asset.kind] || asset.kind));
      if (typeof asset.preview_url === "string" && /^\/api\/projects\/[a-z0-9]+\/input-assets\/[a-z_]+$/.test(asset.preview_url)) {
        const source = typeof studioUrl === "function" ? studioUrl(asset.preview_url) : asset.preview_url;
        const media = element(asset.kind === "reference_audio" ? "audio" : asset.kind === "reference_video" ? "video" : "img");
        media.src = source;
        if (media.tagName === "IMG") { media.alt = roles[asset.kind] || "已绑定输入"; media.loading = "lazy"; }
        else { media.controls = true; media.preload = "metadata"; }
        media.addEventListener("error", () => card.append(element("p", "素材暂不可读取，请核对访问权限与绑定，不能视为首帧已就绪。", "input-material-warning")), {once: true});
        card.append(media);
      }
      card.append(element("span", asset.name || asset.filename || "输入素材"));
      card.append(element("small", asset.asset_id ? `已绑定 · ${asset.asset_id}` : "历史输入素材"));
      if (asset.sha256) card.append(element("small", "SHA256 " + asset.sha256));
      if (asset.kind === "reference_video") {
        const metadata = asset.metadata || {};
        card.append(element("small", metadata.is_cfr_24 === true
          ? `原件直接输入 · 已验证恒定24fps · ${metadata.frame_count}帧 · ${metadata.width}×${metadata.height} · 解码内存单独准入`
          : "原件已保存；尚不符合完整模型的恒定24fps输入要求，未自动转换或提交生成。", metadata.is_cfr_24 === true ? "" : "input-material-warning"));
      }
      grid.append(card);
    }
    container.append(grid);
    if (project.connector_owner) {
      container.append(element("p", "本页仅展示已绑定输入；要换图，请修改输入并重新确认。旧批准不能用于新素材。"));
      if (project.connector_generation_enabled === false) container.append(element("p", "视频生成入口当前关闭：任务尚未提交，不是 GPU 生成失败。", "input-material-warning"));
    }
  }

  window.addEventListener("h3-project-rendered", event => {
    const project = event.detail;
    render(project);
    if (!container?.isConnected || !project?.connector_owner) return;
    let status = container.querySelector("[data-execution-phase]");
    if (!status) { status = element("p"); status.dataset.executionPhase = "true"; container.append(status); }
    const execution = project.stages?.preview?.execution || {};
    const labels = {backend_disabled: "后台已停用，保留排队", backend_starting: "后台启动中", resource_waiting: "资源排队中",
      model_preparing: "模型准备／加载中", input_preparing: "处理已绑定素材", sampling: "正在生成", decoding: "视频／音频解码中",
      saving: "保存视频", completed: "技术完成，等待人工评价", reconciling: "提交结果待对账", cancelling: "正在取消", cancelled: "已取消", error: "执行失败"};
    const timings = Object.entries(execution.phase_timings || {}).filter(([key, value]) => labels[key] && typeof value === "number" && Number.isFinite(value))
      .map(([key, value]) => `${labels[key]} ${Math.round(value)}秒`).join(" · ");
    status.textContent = [labels[execution.phase] || "尚无可核实的执行阶段；进程在线不等于模型已加载", timings].filter(Boolean).join("；");
  });
})();
