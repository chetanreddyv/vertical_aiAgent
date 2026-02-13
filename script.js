const API_URL = 'http://localhost:8001';

class ChatApp {
    constructor() {
        this.messagesContainer = document.getElementById('messagesContainer');
        this.chatForm = document.getElementById('chatForm');
        this.userInput = document.getElementById('userInput');
        this.sendBtn = document.getElementById('sendBtn');
        this.newChatBtn = document.getElementById('newChatBtn');
        this.connectionStatus = document.getElementById('connectionStatus');

        // Auth State
        this.auth = localStorage.getItem('agent_auth') || null;
        this.loginOverlay = document.getElementById('loginOverlay');
        this.loginForm = document.getElementById('loginForm');
        this.logoutBtn = document.getElementById('logoutBtn');

        this.init();
    }

    init() {
        // Event listeners
        this.chatForm.addEventListener('submit', (e) => this.handleSubmit(e));
        this.newChatBtn.addEventListener('click', () => this.newChat());

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

        // Auth listeners
        this.loginForm.addEventListener('submit', (e) => this.handleLogin(e));
        if (this.logoutBtn) {
            this.logoutBtn.addEventListener('click', () => this.logout());
        }

        // Re-check auth on init
        this.checkAuth();

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
        if (!this.auth) return;
        try {
            const response = await fetch(`${API_URL}/health`, {
                headers: { 'Authorization': this.auth }
            });
            const data = await response.json();

            if (data.status === 'healthy') {
                this.connectionStatus.textContent = 'Connected';
            } else {
                this.connectionStatus.textContent = 'Degraded';
            }
        } catch (error) {
            console.error('Health check failed:', error);
            this.connectionStatus.textContent = 'Disconnected';
            this.connectionStatus.parentElement.querySelector('.status-dot').style.background = '#ef4444';
        }
    }

    async checkAuth() {
        if (!this.auth) {
            this.showLogin();
            return;
        }

        try {
            const response = await fetch(`${API_URL}/verify-auth`, {
                headers: { 'Authorization': this.auth }
            });

            if (response.ok) {
                this.hideLogin();
                this.checkHealth();
            } else {
                this.logout();
            }
        } catch (error) {
            console.error('Auth verification failed:', error);
            this.showLogin();
        }
    }

    showLogin() {
        this.loginOverlay.classList.remove('hidden');
        this.loginOverlay.classList.add('flex');
        // Force reflow
        void this.loginOverlay.offsetWidth;
        this.loginOverlay.style.opacity = '1';
        this.loginOverlay.style.pointerEvents = 'auto';
    }

    hideLogin() {
        this.loginOverlay.style.opacity = '0';
        this.loginOverlay.style.pointerEvents = 'none';

        // Wait for transition
        setTimeout(() => {
            this.loginOverlay.classList.add('hidden');
            this.loginOverlay.classList.remove('flex');
        }, 300);
    }

    async handleLogin(e) {
        e.preventDefault();
        const username = document.getElementById('username').value;
        const password = document.getElementById('password').value;
        const errorEl = document.getElementById('loginError');
        const loginBtn = document.getElementById('loginBtn');

        errorEl.classList.add('hidden');
        loginBtn.disabled = true;
        loginBtn.innerHTML = '<span class="animate-spin material-symbols-outlined">sync</span> Checking...';

        const authValue = 'Basic ' + btoa(username + ':' + password);

        try {
            const response = await fetch(`${API_URL}/verify-auth`, {
                headers: { 'Authorization': authValue }
            });

            if (response.ok) {
                this.auth = authValue;
                localStorage.setItem('agent_auth', this.auth);
                this.hideLogin();
                this.checkHealth();
                this.userInput.focus();
            } else {
                errorEl.classList.remove('hidden');
            }
        } catch (error) {
            console.error('Login request failed:', error);
            errorEl.textContent = 'Server unreachable. Check backend.';
            errorEl.classList.remove('hidden');
        } finally {
            loginBtn.disabled = false;
            loginBtn.innerHTML = '<span>Sign In</span><span class="material-symbols-outlined text-[20px]">login</span>';
        }
    }

    logout() {
        this.auth = null;
        localStorage.removeItem('agent_auth');
        window.location.reload(); // Simplest way to reset state
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

        try {
            // Use fetch for streaming to better control the flow and allow POST updates later if needed
            const response = await fetch(`${API_URL}/query-stream?query=${encodeURIComponent(query)}`, {
                headers: { 'Authorization': this.auth }
            });

            if (!response.ok) {
                if (response.status === 401) {
                    this.logout();
                    return;
                }
                throw new Error('Network response was not ok');
            }

            await this.readStream(response.body.getReader(), statusEl, typingId);

        } catch (error) {
            console.error('Query failed:', error);
            this.removeTypingIndicator(typingId);
            if (statusEl) statusEl.remove();
            this.addMessage('assistant', `❌ Failed to connect to backend. Make sure the server is running on ${API_URL}`, 'error');
            this.setLoading(false);
        }
    }

    async readStream(reader, statusEl, typingId) {
        const decoder = new TextDecoder();
        const statusList = statusEl.querySelector('.status-list');

        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                const chunk = decoder.decode(value);
                const lines = chunk.split('\n\n');

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const data = JSON.parse(line.substring(6));

                        if (data.type === 'status') {
                            this.removeTypingIndicator(typingId);
                            statusEl.style.display = 'flex';

                            const statusItem = document.createElement('div');
                            statusItem.className = 'status-item';
                            statusItem.innerHTML = `
                                <span class="status-dot"></span>
                                <span class="status-text">${data.content}</span>
                            `;
                            statusList.appendChild(statusItem);
                            this.scrollToBottom();

                        } else if (data.type === 'step_start') {
                            this.removeTypingIndicator(typingId);
                            statusEl.style.display = 'flex';

                            const statusItem = document.createElement('div');
                            statusItem.className = 'status-item step-start';
                            statusItem.innerHTML = `
                                <div class="step-card">
                                    <div class="step-header">
                                        <span class="step-badge">${data.agent.toUpperCase()}</span>
                                        <strong>Step ${data.step_id}</strong>
                                    </div>
                                    <div class="step-body">
                                        ${this.formatMessage(data.instruction)}
                                    </div>
                                </div>
                            `;
                            statusList.appendChild(statusItem);
                            this.scrollToBottom();

                        } else if (data.type === 'rewritten_query') {
                            const statusItem = document.createElement('div');
                            statusItem.className = 'status-item rewritten-query';
                            statusItem.innerHTML = `
                                <span class="status-dot" style="background: var(--accent);"></span>
                                <span class="status-text">User wants to "<strong>${data.content}</strong>"</span>
                            `;
                            statusList.appendChild(statusItem);
                            this.scrollToBottom();

                        } else if (data.type === 'sql_query') {
                            this.removeTypingIndicator(typingId);
                            statusEl.style.display = 'flex';

                            const explanationItem = document.createElement('div');
                            explanationItem.className = 'status-item';
                            explanationItem.innerHTML = `
                                <span class="status-dot"></span>
                                <span class="status-text">📝 ${this.escapeHtml(data.explanation)}</span>
                            `;
                            statusList.appendChild(explanationItem);

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

                        } else if (data.type === 'confirmation_request') {
                            // Render Confirmation UI
                            this.removeTypingIndicator(typingId);
                            this.renderConfirmationUI(data, statusEl, typingId);
                            return; // Stop reading this stream, wait for user action

                        } else if (data.type === 'result') {
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
                            return;

                        } else if (data.type === 'error') {
                            this.removeTypingIndicator(typingId);
                            statusEl.remove();
                            this.addMessage('assistant', `❌ Error: ${data.error}`, 'error');
                            this.setLoading(false);
                            return;
                        }
                    }
                }
            }
        } catch (error) {
            console.error('Stream reading failed:', error);
            this.removeTypingIndicator(typingId);
            this.addMessage('assistant', '❌ Stream error occurred.', 'error');
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
        if (confirm('Start a new conversation? This will clear the current chat and history.')) {
            // Call backend to clear history
            await this.clearHistory();

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
                method: 'POST',
                headers: { 'Authorization': this.auth }
            });

            if (response.ok) {
                this.addMessage('assistant', '✅ Conversation history has been cleared. Previous context is no longer available.', 'general');
            }
        } catch (error) {
            console.error('Failed to clear history:', error);
            this.addMessage('assistant', '❌ Failed to clear history on the backend.', 'error');
        }
    }
    renderConfirmationUI(data, statusEl, typingId) {
        const statusList = statusEl.querySelector('.status-list');
        const formId = `confirm-form-${Date.now()}`;

        const confirmationItem = document.createElement('div');
        confirmationItem.className = 'status-item confirmation-container';
        confirmationItem.innerHTML = `
            <div class="confirmation-box">
                <div class="confirmation-header">
                    <span class="material-symbols-outlined text-[20px]">warning</span>
                    <span>Approval Required</span>
                </div>
                <div class="confirmation-body">
                    <p>The agent wants to execute the following step:</p>
                    <textarea id="${formId}-instruction" class="confirmation-input">${data.instruction}</textarea>
                    
                    <div class="confirmation-actions">
                        <button class="btn-cancel" id="${formId}-cancel">Cancel</button>
                        <button class="btn-confirm" id="${formId}-confirm">Confirm & Resume</button>
                    </div>
                </div>
            </div>
        `;

        statusList.appendChild(confirmationItem);
        this.scrollToBottom();

        // Add listeners
        document.getElementById(`${formId}-cancel`).addEventListener('click', () => {
            // Treat as user cancellation
            confirmationItem.remove();
            statusEl.remove();
            this.addMessage('assistant', '❌ Action cancelled by user.', 'error');
            this.setLoading(false);
        });

        document.getElementById(`${formId}-confirm`).addEventListener('click', async () => {
            const newInstruction = document.getElementById(`${formId}-instruction`).value;

            // Show loading state
            confirmationItem.innerHTML = `
                <div class="status-item">
                    <span class="status-dot"></span>
                    <span class="status-text">Resuming execution...</span>
                </div>
            `;

            const newTypingId = this.addTypingIndicator(); // Capture the new ID

            try {
                const response = await fetch(`${API_URL}/confirm-step`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': this.auth
                    },
                    body: JSON.stringify({
                        session_id: data.session_id,
                        step_id: data.step_id,
                        approved_instruction: newInstruction,
                        approved_inputs: data.inputs
                    })
                });

                if (!response.ok) throw new Error('Confirmation failed');

                await this.readStream(response.body.getReader(), statusEl, newTypingId);

            } catch (error) {
                console.error('Confirmation failed:', error);
                this.removeTypingIndicator(newTypingId);
                this.addMessage('assistant', '❌ Failed to resume execution.', 'error');
                this.setLoading(false);
            }
        });
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    new ChatApp();
});
