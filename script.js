const API_URL = 'http://localhost:8001';

class ChatApp {
    constructor() {
        this.messagesContainer = document.getElementById('messagesContainer');
        this.chatForm = document.getElementById('chatForm');
        this.userInput = document.getElementById('userInput');
        this.sendBtn = document.getElementById('sendBtn');
        this.newChatBtn = document.getElementById('newChatBtn');
        this.clearHistoryBtn = document.getElementById('clearHistoryBtn');
        this.connectionStatus = document.getElementById('connectionStatus');

        this.init();
    }

    init() {
        // Event listeners
        this.chatForm.addEventListener('submit', (e) => this.handleSubmit(e));
        this.newChatBtn.addEventListener('click', () => this.newChat());
        this.clearHistoryBtn.addEventListener('click', () => this.clearHistory());

        // Mobile Menu Toggle
        const mobileMenuBtn = document.getElementById('mobileMenuBtn');
        if (mobileMenuBtn) {
            mobileMenuBtn.addEventListener('click', () => {
                const sidebar = document.querySelector('.sidebar');
                sidebar.classList.toggle('hidden');
                sidebar.classList.toggle('absolute');
                sidebar.classList.toggle('z-50');
                sidebar.classList.toggle('h-full');
            });
        }

        // Auto-resize textarea
        this.userInput.addEventListener('input', () => this.autoResize());

        // Suggestion chips
        // Event delegation for Suggestion chips
        document.addEventListener('click', (e) => {
            const chip = e.target.closest('.suggestion-chip');
            if (chip) {
                const query = chip.dataset.query;
                if (query) {
                    this.userInput.value = query;
                    this.userInput.focus();
                }
            }

            const dismissBtn = e.target.closest('#dismissSuggestionsBtn');
            if (dismissBtn) {
                const suggestions = dismissBtn.closest('.input-container')?.querySelector('.suggestions') || document.querySelector('.absolute.bottom-0 .suggestions');
                if (suggestions) suggestions.style.display = 'none';
            }
        });

        // Check backend health
        this.checkHealth();
    }

    async checkHealth() {
        try {
            const response = await fetch(`${API_URL}/health`);
            const data = await response.json();

            if (data.status === 'healthy') {
                this.connectionStatus.textContent = 'Connected';
            } else {
                this.connectionStatus.textContent = 'Degraded';
            }
        } catch (error) {
            console.error('Health check failed:', error);
            this.connectionStatus.textContent = 'Disconnected';
            this.connectionStatus.parentElement.querySelector('.status-dot').style.background = 'var(--error)';
        }
    }

    autoResize() {
        this.userInput.style.height = 'auto';
        this.userInput.style.height = this.userInput.scrollHeight + 'px';
    }

    async handleSubmit(e) {
        e.preventDefault();

        const query = this.userInput.value.trim();
        if (!query) return;

        // Hide welcome message
        const welcomeScreen = document.getElementById('welcomeScreen');
        if (welcomeScreen) {
            welcomeScreen.classList.add('hidden');
        }

        // We no longer hide suggestions here, as requested.

        // Add user message
        this.addMessage('user', query);

        // Clear input
        this.userInput.value = '';
        this.userInput.style.height = 'auto';

        // Disable input while processing
        this.setLoading(true);

        // Add typing indicator
        const typingId = this.addTypingIndicator();

        // Create status container immediately
        const statusEl = document.createElement('div');
        statusEl.className = 'message assistant status-message';
        statusEl.id = 'status-display';
        statusEl.style.display = 'none'; // Hide initially
        statusEl.innerHTML = `
            <div class="message-avatar">🤖</div>
            <div class="message-content">
                <div class="status-list"></div>
            </div>
        `;
        this.messagesContainer.appendChild(statusEl);
        const statusList = statusEl.querySelector('.status-list');

        try {
            // Use EventSource for streaming updates
            const eventSource = new EventSource(`${API_URL}/query-stream?query=${encodeURIComponent(query)}`);

            eventSource.onmessage = (event) => {
                const data = JSON.parse(event.data);

                if (data.type === 'status') {
                    // Remove typing indicator on first status
                    this.removeTypingIndicator(typingId);

                    // Show status container
                    statusEl.style.display = 'flex';

                    // Add status item
                    const statusItem = document.createElement('div');
                    statusItem.className = 'status-item';
                    statusItem.innerHTML = `
                        <span class="status-dot"></span>
                        <span class="status-text">${data.content}</span>
                    `;
                    statusList.appendChild(statusItem);
                    statusList.appendChild(statusItem);
                    this.scrollToBottom();

                } else if (data.type === 'rewritten_query') {
                    // Show rewritten query
                    const statusItem = document.createElement('div');
                    statusItem.className = 'status-item rewritten-query';
                    statusItem.innerHTML = `
                        <span class="status-dot" style="background: var(--accent);"></span>
                        <span class="status-text">User wants to "<strong>${data.content}</strong>"</span>
                    `;
                    statusList.appendChild(statusItem);
                    this.scrollToBottom();

                } else if (data.type === 'sql_query') {
                    // Show SQL query immediately with special formatting
                    this.removeTypingIndicator(typingId);
                    statusEl.style.display = 'flex';

                    // Add explanation
                    const explanationItem = document.createElement('div');
                    explanationItem.className = 'status-item';
                    explanationItem.innerHTML = `
                        <span class="status-dot"></span>
                        <span class="status-text">📝 ${this.escapeHtml(data.explanation)}</span>
                    `;
                    statusList.appendChild(explanationItem);

                    // Add SQL query with code formatting
                    const queryItem = document.createElement('div');
                    queryItem.className = 'status-item sql-query-item';
                    queryItem.innerHTML = `
                        <span class="status-dot"></span>
                        <div class="status-text">
                            <strong>🔍 Generated Query:</strong>
                            <pre class="sql-query-code"><code>${this.escapeHtml(data.query)}</code></pre>
                        </div>
                    `;
                    statusList.appendChild(queryItem);
                    this.scrollToBottom();

                } else if (data.type === 'result') {
                    // Handle final result
                    eventSource.close();

                    // Wait a moment then remove status and show result
                    setTimeout(() => {
                        statusEl.remove();
                        if (data.data.success) {
                            this.addMessage('assistant', data.data.response, data.data.intent, data.data.sql_data);
                        } else {
                            this.addMessage('assistant', `❌ Error: ${data.data.error || 'Unknown error'}`, 'error');
                        }
                        this.setLoading(false);
                        this.userInput.focus();
                    }, 500);

                } else if (data.type === 'error') {
                    eventSource.close();
                    this.removeTypingIndicator(typingId);
                    statusEl.remove();
                    this.addMessage('assistant', `❌ Error: ${data.error}`, 'error');
                    this.setLoading(false);
                }
            };

            eventSource.onerror = (error) => {
                console.error('EventSource failed:', error);
                eventSource.close();
                this.removeTypingIndicator(typingId);
                statusEl.remove();
                this.addMessage('assistant', '❌ Connection lost. Please try again.', 'error');
                this.setLoading(false);
            };

        } catch (error) {
            console.error('Query failed:', error);
            this.removeTypingIndicator(typingId);
            if (statusEl) statusEl.remove();
            this.addMessage('assistant', `❌ Failed to connect to backend. Make sure the server is running on ${API_URL}`, 'error');
            this.setLoading(false);
        }
    }

    async showStatusUpdates(updates) {
        const statusEl = document.createElement('div');
        statusEl.className = 'message assistant status-message';
        statusEl.id = 'status-display';
        statusEl.innerHTML = `
            <div class="message-avatar">🤖</div>
            <div class="message-content">
                <div class="status-list"></div>
            </div>
        `;

        this.messagesContainer.appendChild(statusEl);
        const statusList = statusEl.querySelector('.status-list');

        // Show each status update with a delay
        for (let i = 0; i < updates.length; i++) {
            const statusItem = document.createElement('div');
            statusItem.className = 'status-item';
            statusItem.innerHTML = `
                <span class="status-dot"></span>
                <span class="status-text">${updates[i]}</span>
            `;
            statusList.appendChild(statusItem);
            this.scrollToBottom();

            // Wait a bit before showing next status (faster for better UX)
            await new Promise(resolve => setTimeout(resolve, 300));
        }

        // Wait a moment before removing status and showing result
        await new Promise(resolve => setTimeout(resolve, 500));
        statusEl.remove();
    }

    addMessage(role, content, intent = null, sqlData = null) {
        const messageEl = document.createElement('div');
        messageEl.className = `message ${role}`;

        const avatar = role === 'user' ? '👤' : '🤖';
        const name = role === 'user' ? 'You' : 'Assistant';

        let intentBadge = '';
        if (intent && role === 'assistant') {
            const intentIcons = {
                'email_only': '📧',
                'sql_only': '🗄️',
                'email_and_sql': '🔄',
                'drive_only': '📁',
                'calendar_only': '📅',
                'tldv_only': '🗒️',
                'multi_workspace': '🌐',
                'general': '💬'
            };
            const icon = intentIcons[intent] || '💬';
            const intentLabel = intent.replace(/_/g, ' ').toUpperCase();
            intentBadge = `<span class="intent-badge">${icon} ${intentLabel}</span>`;
        }

        // Handle SQL data if available
        let messageContent = this.formatMessage(content);
        if (sqlData && sqlData.rows && sqlData.rows.length > 0) {
            // Extract explanation and query from the content
            const parts = content.split('\n\n');
            let explanation = '';
            let query = '';

            // Prefer data from backend if available
            if (sqlData.executed_explanation && sqlData.executed_query) {
                explanation = `📝 Explanation: ${sqlData.executed_explanation}`;
                query = `🔍 Query:\n\`\`\`sql\n${sqlData.executed_query}\n\`\`\``;
            } else {
                // Fallback to parsing text
                for (const part of parts) {
                    if (part.startsWith('📝 Explanation:')) {
                        explanation = part;
                    } else if (part.startsWith('🔍 Query:')) {
                        query = part;
                    }
                }
            }

            // Rebuild content with HTML table
            const explanationHtml = this.formatMessage(explanation);
            const queryHtml = this.formatMessage(query);
            const tableHtml = this.createSQLTable(sqlData);

            messageContent = `${explanationHtml}<br><br>${queryHtml}<br><br>${tableHtml}`;
        }

        messageEl.innerHTML = `
            <div class="message-avatar">${avatar}</div>
            <div class="message-content">
                <div class="message-header">
                    <span class="message-name">${name}</span>
                    ${intentBadge}
                </div>
                <div class="message-text">${messageContent}</div>
            </div>
        `;

        this.messagesContainer.appendChild(messageEl);
        this.scrollToBottom();
    }

    createSQLTable(sqlData) {
        if (!sqlData || !sqlData.rows || sqlData.rows.length === 0) {
            return '<p>No results found.</p>';
        }

        const rows = sqlData.rows;
        const columns = Object.keys(rows[0]);

        let tableHtml = '<div class="sql-table-container">';
        tableHtml += '<table class="sql-table">';

        // Header
        tableHtml += '<thead><tr>';
        columns.forEach(col => {
            tableHtml += `<th>${this.escapeHtml(col)}</th>`;
        });
        tableHtml += '</tr></thead>';

        // Body
        tableHtml += '<tbody>';
        rows.forEach(row => {
            tableHtml += '<tr>';
            columns.forEach(col => {
                const value = row[col] !== null && row[col] !== undefined ? row[col] : '';
                tableHtml += `<td>${this.escapeHtml(String(value))}</td>`;
            });
            tableHtml += '</tr>';
        });
        tableHtml += '</tbody>';

        tableHtml += '</table>';
        tableHtml += `<div class="table-footer">📊 Total rows: ${rows.length}</div>`;
        tableHtml += '</div>';

        return tableHtml;
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    formatMessage(content) {
        if (!content) return '';

        // 1. Extract code blocks to prevent formatting inside them
        const codeBlocks = [];
        let processedContent = content.replace(/```(\w*)\n?([\s\S]*?)```/g, (match, lang, code) => {
            codeBlocks.push({ lang, code });
            return `__CODE_BLOCK_${codeBlocks.length - 1}__`;
        });

        // Also handle inline code
        processedContent = processedContent.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');

        // 2. Escape HTML characters (basic security)
        processedContent = processedContent
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

        // 3. Process Markdown Lines
        const lines = processedContent.split('\n');
        let htmlLines = [];
        let inList = false;
        let listType = null; // 'ul' or 'ol'

        for (let i = 0; i < lines.length; i++) {
            let line = lines[i];

            // Check for headers
            if (line.startsWith('### ')) {
                if (inList) { htmlLines.push(listType === 'ul' ? '</ul>' : '</ol>'); inList = false; }
                htmlLines.push(`<h3>${this.parseInline(line.substring(4))}</h3>`);
                continue;
            } else if (line.startsWith('## ')) {
                if (inList) { htmlLines.push(listType === 'ul' ? '</ul>' : '</ol>'); inList = false; }
                htmlLines.push(`<h2>${this.parseInline(line.substring(3))}</h2>`);
                continue;
            } else if (line.startsWith('# ')) {
                if (inList) { htmlLines.push(listType === 'ul' ? '</ul>' : '</ol>'); inList = false; }
                htmlLines.push(`<h1>${this.parseInline(line.substring(2))}</h1>`);
                continue;
            }

            // Check for Lists
            // Unordered list (- or *)
            const ulMatch = line.match(/^\s*[-*]\s+(.*)/);
            // Ordered list (1.)
            const olMatch = line.match(/^\s*\d+\.\s+(.*)/);

            if (ulMatch || olMatch) {
                const currentType = ulMatch ? 'ul' : 'ol';
                const content = ulMatch ? ulMatch[1] : olMatch[1];

                if (!inList) {
                    htmlLines.push(`<${currentType}>`);
                    inList = true;
                    listType = currentType;
                } else if (listType !== currentType) {
                    // Switch list type (close old, open new)
                    htmlLines.push(listType === 'ul' ? '</ul>' : '</ol>');
                    htmlLines.push(`<${currentType}>`);
                    listType = currentType;
                }

                htmlLines.push(`<li>${this.parseInline(content)}</li>`);
            } else {
                if (inList && line.trim() === '') {
                    continue;
                }

                // Not a list item
                if (inList) {
                    htmlLines.push(listType === 'ul' ? '</ul>' : '</ol>');
                    inList = false;
                    listType = null;
                }

                // Handle empty lines or regular text
                if (line.trim() === '') {
                    htmlLines.push('<br>');
                } else {
                    htmlLines.push(`<p>${this.parseInline(line)}</p>`);
                }
            }
        }

        if (inList) {
            htmlLines.push(listType === 'ul' ? '</ul>' : '</ol>');
        }

        // Reassemble
        let html = htmlLines.join('');

        // 4. Restore code blocks
        html = html.replace(/__CODE_BLOCK_(\d+)__/g, (match, index) => {
            const block = codeBlocks[index];
            return `<pre><code class="language-${block.lang}">${block.code}</code></pre>`;
        });

        // Cleanup multiple BRs
        html = html.replace(/(<br>){3,}/g, '<br><br>');

        return html;
    }

    parseInline(text) {
        return text
            // Bold
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            // Italic
            .replace(/\*(.*?)\*/g, '<em>$1</em>')
            // Links
            .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    }

    addTypingIndicator() {
        const typingEl = document.createElement('div');
        const typingId = 'typing-' + Date.now();
        typingEl.id = typingId;
        typingEl.className = 'message assistant';
        typingEl.innerHTML = `
            <div class="message-avatar">🤖</div>
            <div class="message-content">
                <div class="typing-indicator">
                    <div class="typing-dot"></div>
                    <div class="typing-dot"></div>
                    <div class="typing-dot"></div>
                </div>
            </div>
        `;
        this.messagesContainer.appendChild(typingEl);
        this.scrollToBottom();
        return typingId;
    }

    removeTypingIndicator(typingId) {
        const typingEl = document.getElementById(typingId);
        if (typingEl) {
            typingEl.remove();
        }
    }

    setLoading(isLoading) {
        this.sendBtn.disabled = isLoading;
        this.userInput.disabled = isLoading;
    }

    scrollToBottom() {
        this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
    }

    async newChat() {
        if (confirm('Start a new conversation? This will clear the current chat.')) {
            // Remove all existing messages
            const messages = this.messagesContainer.querySelectorAll('.message');
            messages.forEach(msg => msg.remove());

            // Show welcome screen
            const welcomeScreen = document.getElementById('welcomeScreen');
            if (welcomeScreen) {
                welcomeScreen.classList.remove('hidden');
            }

            // Clear input
            this.userInput.value = '';
            this.userInput.style.height = 'auto';
        }
    }

    async clearHistory() {
        try {
            const response = await fetch(`${API_URL}/reset-history`, {
                method: 'POST'
            });

            if (response.ok) {
                this.addMessage('assistant', '✅ Conversation history has been cleared. Previous context is no longer available.', 'general');
            }
        } catch (error) {
            console.error('Failed to clear history:', error);
            this.addMessage('assistant', '❌ Failed to clear history on the backend.', 'error');
        }
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    new ChatApp();
});
