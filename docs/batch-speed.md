# 批量注册提速

基于 `register_cli` + 浏览器注册 + 热浏览器 CPA mint。  
**约束**：Turnstile token 不可跨账号复用；直连多线程易被 Cloudflare 连拦。

---

## 1. 单账号时间模型（headed、成功路径）

| 阶段 | 大约耗时 | 说明 |
| --- | --- | --- |
| 打开注册页 | 2–5 s | 导航 + DOM |
| 临时邮箱 + 填邮箱 | 1–3 s | API 与 DOM 可重叠；预创建池可再削延迟 |
| 验证码 OTP | 1–5 s | TempMail 等通常较快 |
| **资料页 + Turnstile** | **8–25 s** | 主瓶颈 |
| SSO cookie | 2–5 s | 登录完成 |
| **CPA mint** | **12–25 s** | 设备码 OAuth + 同意（热浏览器） |
| 会话清理 | 0.5–2 s | |
| **合计** | **~35–70 s/号** | CF 严时更长 |

成功日志示例：

```text
[*] timing: init=3200ms email=900ms otp=1500ms profile=14000ms sso=2800ms total=22400ms
+ 注册成功: xxx@… (via browser) | init=… total=22400ms
```

吞吐（单线程）约 \(3600 / T_{\text{单号}}\) 账号/小时。

---

## 2. 瓶颈

1. **Turnstile 每号必解**（单次、约 300s 有效）  
2. **CPA mint 串在注册后**（热浏览器过 CF，难以与下一号并行）  
3. **直连多线程** → 同 IP 多 Chrome，CF 失败率上升  

---

## 3. 推荐姿势

```bash
# 直连：单线程 + 有界面（最稳）
python register_cli.py --extra 10 --threads 1 --headed

# 代理池探测可用后再多线程
python register_cli.py --extra 20 --threads 2 --headed --proxy-pool "..."
```

建议配置：

```json
{
  "code_poll_timeout": 20,
  "code_poll_interval": 3,
  "mail_pool_size": 3,
  "cpa_mint_prefer_warm_browser": true,
  "cpa_probe_after_write": true,
  "cf_prewarm": "auto",
  "thread_start_interval": 1.5
}
```

- `mail_pool_size`：1–10，`0` 关闭  
- TempMail 收信间隔代码侧 **≥3s**（服务商限流）  
- CLI 默认 `--fast`：压缩 sleep、OTP、Turnstile 协助时机  

---

## 4. 当前已实现的提速手段

| 手段 | 作用 |
| --- | --- |
| `sleep_scale`（fast≈0.15） | 缩短可缩放等待 |
| Turnstile 先读再协助 | 避免无故 reset；fast 下更早介入 |
| 邮箱 API ∥ DOM 就绪 | 省 0.5–2s/号 |
| `mail_pool` 预创建 | 再省邮箱 API 等待 |
| 直连自动 `threads→1` | 保住有效成功率 |
| `stage_summary` 日志 | 定位瓶颈阶段 |

---

## 5. 不建议

| 做法 | 原因 |
| --- | --- |
| 跨账号复用 Turnstile | 会失败 |
| 无头多开直连硬冲 | 总吞吐更差 |
| 冷浏览器并行 mint | 易 CF block |
| `sleep_scale` 接近 0 | DOM/扩展来不及 |

---

## 6. 注册 vs CPA

注册成功 ≠ CPA 成功。补 mint：

```bash
python register_cli.py --remint-missing --headed
```

详见 [export-cpa-and-sub2api.md](export-cpa-and-sub2api.md)。

---

## 7. 一句话

主瓶颈是 **每号 Turnstile + 串行热 mint**。能做的是减空等、重叠 IO、预创建邮箱、headed 单线程跑满成功率；有干净代理再谈多线程。
