const chatEl = document.getElementById('chat');
const formEl = document.getElementById('composer');
const inputEl = document.getElementById('input');
const sendBtn = formEl.querySelector('button');

/** @type {{role:'user'|'assistant', content:string}[]} */
let history = [];
let firstMessage = true;

function addBubble(role, text) {
  // 首次对话时清除欢迎界面
  if (firstMessage) {
    chatEl.innerHTML = '';
    firstMessage = false;
  }

  const row = document.createElement('div');
  row.className = `row ${role === 'user' ? 'me' : 'ai'}`;

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = formatContent(text);

  row.appendChild(bubble);
  chatEl.appendChild(row);
  scrollToBottom();
  return bubble;
}

/**
 * 简单 Markdown 渲染：处理标题、粗体、链接、列表等
 */
function formatContent(text) {
  if (!text) return '';

  let html = text
    // 转义 HTML
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    // 链接
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    // 粗体
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    // 行内代码
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // ### 标题
    .replace(/^### (.+)$/gm, '<h4>$1</h4>')
    // ## 标题
    .replace(/^## (.+)$/gm, '<h3>$1</h3>')
    // # 标题
    .replace(/^# (.+)$/gm, '<h2>$1</h2>')
    // 无序列表
    .replace(/^- (.+)$/gm, '<li style="margin-left:16px">$1</li>')
    // 有序列表
    .replace(/^\d+\.\s(.+)$/gm, '<li style="margin-left:16px">$1</li>')
    // URL 自动链接（非 Markdown 链接的）
    .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
    // 换行
    .replace(/\n/g, '<br>');

  return html;
}

function scrollToBottom() {
  chatEl.scrollTop = chatEl.scrollHeight;
}

async function sendMessage(message) {
  sendBtn.disabled = true;
  try {
    addBubble('user', message);

    const aiBubble = addBubble('assistant', '');

    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, history }),
    });

    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data?.error || resp.statusText || '请求失败');
    }
    if (!resp.body) {
      throw new Error('当前浏览器不支持流式读取（response.body 为空）');
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');

    let buffer = '';
    let fullText = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE 事件用空行分隔：\n\n
      const events = buffer.split('\n\n');
      buffer = events.pop() || '';

      for (const evt of events) {
        const dataLine = evt.split('\n').find(l => l.startsWith('data: '));
        if (!dataLine) continue;

        let payload;
        try {
          payload = JSON.parse(dataLine.slice('data: '.length));
        } catch {
          continue;
        }

        if (payload.type === 'token') {
          fullText += String(payload.token || '');
          aiBubble.innerHTML = formatContent(fullText);
          scrollToBottom();
        } else if (payload.type === 'error') {
          throw new Error(payload.message || '服务端错误');
        } else if (payload.type === 'done') {
          // done 不一定是最后一个分片，但这里可以忽略
        }
      }
    }

    history.push({ role: 'user', content: message });
    history.push({ role: 'assistant', content: fullText });

  } catch (e) {
    addBubble('assistant', `❌ 错误：${e.message}`);
  } finally {
    sendBtn.disabled = false;
  }
}

formEl.addEventListener('submit', (e) => {
  e.preventDefault();
  const msg = inputEl.value.trim();
  if (!msg) return;
  inputEl.value = '';
  sendMessage(msg);
});

// 点击推荐标签自动填入
chatEl.addEventListener('click', (e) => {
  const tag = e.target.closest('.tag');
  if (tag) {
    inputEl.value = tag.textContent.replace('试试：', '').trim();
    inputEl.focus();
  }
});

inputEl.focus();
