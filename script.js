const API_URL = 'http://localhost:8000';

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

        // Auto-resize textarea
        this.userInput.addEventListener('input', () => this.autoResize());

        // Suggestion chips
        document.querySelectorAll('.suggestion-chip').forEach(chip => {
            chip.addEventListener('click', (e) => {
                const query = e.target.dataset.query;
                this.userInput.value = query;
                this.userInput.focus();
            });
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
                this.updateAgentStatus(data.agents);
            } else {
                this.connectionStatus.textContent = 'Degraded';
            }
        } catch (error) {
            console.error('Health check failed:', error);
            this.connectionStatus.textContent = 'Disconnected';
            this.connectionStatus.parentElement.querySelector('.status-dot').style.background = 'var(--error)';
        }
    }

    updateAgentStatus(agents) {
        Object.entries(agents).forEach(([agent, status]) => {
            const badge = document.querySelector(`[data-agent="${agent}"]`);
            if (badge && status === 'ready') {
                badge.classList.add('active');
            }
        });
    }

    autoResize() {
        this.userInput.style.height = 'auto';
        this.userInput.style.height = this.userInput.scrollHeight + 'px';
    }

    async handleSubmit(e) {
        e.preventDefault();

        const query = this.userInput.value.trim();
        if (!query) return;

        // Clear welcome message if it exists
        const welcomeMessage = document.querySelector('.welcome-message');
        if (welcomeMessage) {
            welcomeMessage.remove();
        }

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
                'docs_only': '📝',
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

            for (const part of parts) {
                if (part.startsWith('📝 Explanation:')) {
                    explanation = part;
                } else if (part.startsWith('🔍 Query:')) {
                    query = part;
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
        // Simple formatting for code blocks and line breaks
        return content
            .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\n/g, '<br>');
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
            this.messagesContainer.innerHTML = `
                <div class="welcome-message">
                    <div class="welcome-icon">✨</div>
                    <h2>Welcome to AI Agent Assistant</h2>
                    <p>I can help you with email, database queries, file management, calendar scheduling, and more.</p>
                    <div class="suggestions">
                        <button class="suggestion-chip" data-query="List my Google Drive files">
                            📁 List Drive files
                        </button>
                        <button class="suggestion-chip" data-query="Upload a file to Google Drive">
                            📤 Upload to Drive
                        </button>
                        <button class="suggestion-chip" data-query="Create a folder in Google Drive">
                            📂 Create Drive folder
                        </button>
                    </div>
                </div>
            `;

            // Re-attach suggestion listeners
            document.querySelectorAll('.suggestion-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    const query = e.target.dataset.query;
                    this.userInput.value = query;
                    this.userInput.focus();
                });
            });
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
