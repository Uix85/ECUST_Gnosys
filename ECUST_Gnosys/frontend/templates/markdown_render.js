<script>
// ═══════════════════════════════════════════════════════
// 统一 Markdown 渲染（window.renderMarkdown）
// 供阅读页弹窗、笔记界面各层级内容共用：调取内容后直接渲染可读。
// 安全：先 HTML 转义，再转换基础 Markdown 语法。
// 支持：#~###### 标题、**粗体**、*斜体*、`行内代码`、```代码块```、
//       - 无序列表、1. 有序列表、[链接](url)、--- 分割线、段落。
// ⚠ 本文件被 Jinja2 include 引入：文件内不要出现连续两个左花括号，
//   也不要出现 Jinja 标签定界符，以免被模板引擎解析。
// ═══════════════════════════════════════════════════════
(function() {
    function esc(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    // 行内样式：代码 → 粗体 → 斜体 → 链接
    function inline(s) {
        s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
        s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
        s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
        return s;
    }

    window.renderMarkdown = function(md) {
        if (md === null || md === undefined) return '';
        var text = String(md).replace(/\r\n/g, '\n');

        // 1) 提取代码块，避免内部被段落化
        var blocks = [];
        text = text.replace(/```([\s\S]*?)```/g, function(m, code) {
            var idx = blocks.length;
            blocks.push('<pre><code>' + esc(code.replace(/\n+$/, '')) + '</code></pre>');
            return '\u0000BLOCK' + idx + '\u0000';
        });

        var lines = text.split('\n');
        var html = [];
        var listStack = null;      // 'ul' | 'ol' | null
        var para = [];

        function flushPara() {
            if (para.length) {
                html.push('<p>' + inline(esc(para.join(' '))) + '</p>');
                para = [];
            }
        }
        function closeList() {
            if (listStack) { html.push('</' + listStack + '>'); listStack = null; }
        }

        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            var t = line.trim();

            var blk = t.match(/^\u0000BLOCK(\d+)\u0000$/);
            if (blk) { flushPara(); closeList(); html.push(blocks[+blk[1]]); continue; }

            if (!t) { flushPara(); closeList(); continue; }

            var hm = t.match(/^(#{1,6})\s+(.*)$/);
            if (hm) {
                flushPara(); closeList();
                var lv = hm[1].length;
                html.push('<h' + lv + '>' + inline(esc(hm[2])) + '</h' + lv + '>');
                continue;
            }

            var um = t.match(/^[-*]\s+(.*)$/);
            if (um) {
                flushPara();
                if (listStack !== 'ul') { closeList(); html.push('<ul>'); listStack = 'ul'; }
                html.push('<li>' + inline(esc(um[1])) + '</li>');
                continue;
            }

            var om = t.match(/^\d+[.、]\s+(.*)$/);
            if (om) {
                flushPara();
                if (listStack !== 'ol') { closeList(); html.push('<ol>'); listStack = 'ol'; }
                html.push('<li>' + inline(esc(om[1])) + '</li>');
                continue;
            }

            if (/^[-*_]{3,}$/.test(t)) { flushPara(); closeList(); html.push('<hr>'); continue; }

            if (listStack) closeList();
            para.push(t);
        }
        flushPara();
        closeList();

        var out = html.join('\n');
        // 2) 还原代码块
        out = out.replace(/\u0000BLOCK(\d+)\u0000/g, function(m, n) { return blocks[+n]; });
        return out;
    };
})();
</script>
