// Turnstile Patch - 隐藏自动化标识，加速 Turnstile 验证
// 在 document_start 阶段执行，确保在页面脚本之前生效
// all_frames:true — also runs inside challenge iframes when permitted

(function () {
    "use strict";

    // 1. 隐藏 navigator.webdriver 标识
    try {
        Object.defineProperty(navigator, "webdriver", {
            get: function () {
                return undefined;
            },
            configurable: true,
        });
    } catch (e) {}
    try {
        var proto = Navigator && Navigator.prototype;
        if (proto) {
            Object.defineProperty(proto, "webdriver", {
                get: function () {
                    return undefined;
                },
                configurable: true,
            });
        }
    } catch (e) {}

    // 2. 移除 Chrome 自动化相关的 Runtime 属性
    try {
        if (window.chrome && window.chrome.runtime) {
            delete window.chrome.runtime.onConnect;
            delete window.chrome.runtime.onMessage;
        }
    } catch (e) {}

    // 3. 覆盖 permissions.query，隐藏 notifications 权限异常
    try {
        var origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = function (params) {
            if (params && params.name === "notifications") {
                return Promise.resolve({ state: Notification.permission });
            }
            return origQuery(params);
        };
    } catch (e) {}

    // 4. 修补 plugin 数量，模拟正常浏览器
    try {
        Object.defineProperty(navigator, "plugins", {
            get: function () {
                return [1, 2, 3, 4, 5];
            },
            configurable: true,
        });
    } catch (e) {}

    // 5. 修补 languages 属性
    try {
        Object.defineProperty(navigator, "languages", {
            get: function () {
                return ["en-US", "en"];
            },
            configurable: true,
        });
    } catch (e) {}

    // 6. Patch MouseEvent screen coordinates (some CF probes check these)
    try {
        function rnd(min, max) {
            return Math.floor(Math.random() * (max - min + 1)) + min;
        }
        Object.defineProperty(MouseEvent.prototype, "screenX", {
            get: function () {
                return rnd(600, 1600);
            },
        });
        Object.defineProperty(MouseEvent.prototype, "screenY", {
            get: function () {
                return rnd(200, 1000);
            },
        });
    } catch (e) {}

    // 7. Auto-monitor checkbox / widget click
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", autoClickTurnstile);
    } else {
        autoClickTurnstile();
    }

    function tryClick(el) {
        if (!el) return false;
        try {
            el.click();
            return true;
        } catch (e) {
            return false;
        }
    }

    function autoClickTurnstile() {
        var checkCount = 0;
        var maxChecks = 120; // ~60s
        var timer = setInterval(function () {
            checkCount++;
            if (checkCount > maxChecks) {
                clearInterval(timer);
                return;
            }
            try {
                // Inside challenge frame: click the real checkbox when accessible
                var localBox = document.querySelector(
                    'input[type="checkbox"], .mark, #challenge-stage input, label.cb-lb input'
                );
                if (localBox && !localBox.checked) {
                    tryClick(localBox);
                }

                // Host page: Turnstile iframes
                var iframes = document.querySelectorAll(
                    'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
                );
                for (var i = 0; i < iframes.length; i++) {
                    var iframe = iframes[i];
                    try {
                        var body =
                            iframe.contentDocument ||
                            (iframe.contentWindow && iframe.contentWindow.document);
                        if (body) {
                            var checkbox = body.querySelector(
                                'input[type="checkbox"], .mark, #cf-chl-widget-nomu1_resp, label input'
                            );
                            if (checkbox && !checkbox.checked) {
                                tryClick(checkbox);
                            }
                        }
                    } catch (e) {
                        // Cross-origin — still click the iframe element (focus/activation)
                        tryClick(iframe);
                        try {
                            iframe.contentWindow &&
                                iframe.contentWindow.postMessage(
                                    { type: "turnstile-auto-click" },
                                    "*"
                                );
                        } catch (e2) {}
                    }
                }

                // Host containers
                var containers = document.querySelectorAll(
                    "div.cf-turnstile, [data-sitekey]"
                );
                for (var j = 0; j < containers.length; j++) {
                    tryClick(containers[j]);
                }

                if (
                    window.turnstile &&
                    typeof window.turnstile.getResponse === "function"
                ) {
                    var resp = window.turnstile.getResponse();
                    if (resp && resp.length > 0) {
                        clearInterval(timer);
                    }
                }
            } catch (e) {}
        }, 500);
    }
})();
