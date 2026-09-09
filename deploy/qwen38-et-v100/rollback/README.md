# 回滚到 QUASAR-QAT（切换前原状）

快照时间：2026-09-09。切换只动了 systemd 两个 user unit 的
EnvironmentFile / ExecStart / Description 三行；QUASAR 模型文件
（/home/ai/model-sources/modelscope/QUASAR-QAT/...）与本目录备份均未改动。

```bash
R=/home/ai/github/1Panel/deploy/qwen38-et-v100
systemctl --user stop qwen38-v100-tp2-vllm   # BindsTo 会连带停 lmcache
cp "$R/rollback/qwen38-v100-tp2-vllm.service.orig-20260909"   ~/.config/systemd/user/qwen38-v100-tp2-vllm.service
cp "$R/rollback/qwen38-v100-tp2-lmcache.service.orig-20260909" ~/.config/systemd/user/qwen38-v100-tp2-lmcache.service
systemctl --user daemon-reload
systemctl --user start qwen38-v100-tp2-vllm
# 原 env 文件仍在 ~/.config/1cat-vllm/qwen38-v100-tp2.env，从未被修改
```

验证：`curl -H "Authorization: Bearer $(cat ~/.config/1cat-vllm/api-key)" \
http://127.0.0.1:18107/v1/models`
