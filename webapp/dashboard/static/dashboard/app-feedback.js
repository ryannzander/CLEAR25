/* ============================================================
   PM2.5 EWS — Feedback board: list, sort, voting, detail view
   ============================================================ */

let feedbackAuth = { authenticated: false, username: "" };
let currentSort = "hot";
let currentSuggestionId = null;

function initFeedbackBoard() {
    fetch("/api/auth-status/")
        .then(r => r.json())
        .then(data => { feedbackAuth = data; })
        .catch(() => {});

    // Sort buttons
    document.querySelectorAll(".sort-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".sort-btn").forEach(b => b.classList.remove("sort-active"));
            btn.classList.add("sort-active");
            currentSort = btn.dataset.sort;
            loadSuggestions();
        });
    });

    // New suggestion button
    document.getElementById("btn-new-suggestion")?.addEventListener("click", () => {
        if (!feedbackAuth.authenticated) {
            document.getElementById("modal-login").style.display = "flex";
            return;
        }
        document.getElementById("suggestion-title").value = "";
        document.getElementById("suggestion-body").value = "";
        document.getElementById("suggestion-error").style.display = "none";
        document.getElementById("modal-suggestion").style.display = "flex";
    });

    // Modal closes
    document.getElementById("modal-suggestion-close")?.addEventListener("click", () => {
        document.getElementById("modal-suggestion").style.display = "none";
    });
    document.getElementById("modal-suggestion-cancel")?.addEventListener("click", () => {
        document.getElementById("modal-suggestion").style.display = "none";
    });
    document.getElementById("modal-detail-close")?.addEventListener("click", () => {
        document.getElementById("modal-detail").style.display = "none";
    });
    document.getElementById("modal-login-close")?.addEventListener("click", () => {
        document.getElementById("modal-login").style.display = "none";
    });

    // Close modals on overlay click
    ["modal-suggestion", "modal-detail", "modal-login"].forEach(id => {
        document.getElementById(id)?.addEventListener("click", (e) => {
            if (e.target.classList.contains("modal-overlay")) e.target.style.display = "none";
        });
    });

    // Submit suggestion
    document.getElementById("modal-suggestion-submit")?.addEventListener("click", submitSuggestion);

    // Vote buttons in detail modal
    document.getElementById("detail-upvote")?.addEventListener("click", () => voteSuggestion(1));
    document.getElementById("detail-downvote")?.addEventListener("click", () => voteSuggestion(-1));

    // Add comment & delete
    document.getElementById("btn-add-comment")?.addEventListener("click", addComment);
    document.getElementById("btn-delete-suggestion")?.addEventListener("click", deleteSuggestion);

    loadSuggestions();
}

async function loadSuggestions() {
    const list = document.getElementById("suggestions-list");
    if (!list) return;

    try {
        const resp = await fetch(`/api/suggestions/?sort=${currentSort}`);
        const data = await resp.json();

        if (!data.suggestions || data.suggestions.length === 0) {
            list.innerHTML = `<div class="suggestions-empty">No suggestions yet. Be the first to share an idea!</div>`;
            return;
        }

        list.innerHTML = data.suggestions.map(s => `
            <div class="suggestion-card" data-id="${s.id}">
                <div class="suggestion-votes">
                    <button class="vote-btn vote-up ${s.user_vote === 1 ? 'voted-up' : ''}" data-id="${s.id}" data-vote="1">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg>
                    </button>
                    <span class="suggestion-score">${s.score}</span>
                    <button class="vote-btn vote-down ${s.user_vote === -1 ? 'voted-down' : ''}" data-id="${s.id}" data-vote="-1">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                </div>
                <img src="${escapeHtml(s.author_avatar)}" alt="" class="suggestion-avatar">
                <div class="suggestion-content">
                    <div class="suggestion-title">${escapeHtml(s.title)}</div>
                    <div class="suggestion-meta">
                        <span class="suggestion-author">${escapeHtml(s.author)}</span>
                        <span>${timeAgo(s.created_at)}</span>
                        <span>${s.comment_count} comment${s.comment_count !== 1 ? 's' : ''}</span>
                    </div>
                </div>
            </div>
        `).join("");

        list.querySelectorAll(".suggestion-card").forEach(card => {
            card.addEventListener("click", (e) => {
                if (e.target.closest(".vote-btn")) return;
                openSuggestionDetail(parseInt(card.dataset.id));
            });
        });

        list.querySelectorAll(".vote-btn").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const id = parseInt(btn.dataset.id);
                const vote = parseInt(btn.dataset.vote);
                quickVote(id, vote, btn);
            });
        });
    } catch (e) {
        list.innerHTML = `<div class="suggestions-empty">Failed to load suggestions</div>`;
    }
}

async function quickVote(suggestionId, value, btn) {
    if (!feedbackAuth.authenticated) {
        document.getElementById("modal-login").style.display = "flex";
        return;
    }

    const card = btn.closest(".suggestion-card");
    const scoreEl = card.querySelector(".suggestion-score");
    const upBtn = card.querySelector(".vote-up");
    const downBtn = card.querySelector(".vote-down");
    const wasVoted = btn.classList.contains(value === 1 ? "voted-up" : "voted-down");
    const newValue = wasVoted ? 0 : value;

    try {
        const resp = await fetch(`/api/suggestions/${suggestionId}/vote/`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: newValue }),
        });
        const data = await resp.json();
        if (resp.ok) {
            scoreEl.textContent = data.score;
            upBtn.classList.toggle("voted-up", data.user_vote === 1);
            downBtn.classList.toggle("voted-down", data.user_vote === -1);
        }
    } catch (e) {
        logger.error("Vote failed:", e);
    }
}

async function openSuggestionDetail(id) {
    currentSuggestionId = id;
    const modal = document.getElementById("modal-detail");

    try {
        const resp = await fetch(`/api/suggestions/${id}/`);
        const s = await resp.json();

        document.getElementById("detail-title").textContent = s.title;
        document.getElementById("detail-avatar").src = s.author_avatar;
        document.getElementById("detail-author").textContent = s.author;
        document.getElementById("detail-date").textContent = timeAgo(s.created_at);
        document.getElementById("detail-body").textContent = s.body;
        document.getElementById("detail-score").textContent = s.score;

        const upBtn = document.getElementById("detail-upvote");
        const downBtn = document.getElementById("detail-downvote");
        upBtn.classList.toggle("voted-up", s.user_vote === 1);
        downBtn.classList.toggle("voted-down", s.user_vote === -1);

        const commentsEl = document.getElementById("detail-comments");
        if (s.comments.length === 0) {
            commentsEl.innerHTML = `<div class="comments-empty">No comments yet</div>`;
        } else {
            commentsEl.innerHTML = s.comments.map(c => `
                <div class="comment-item">
                    <img src="${escapeHtml(c.author_avatar)}" alt="" class="comment-avatar">
                    <div class="comment-content">
                        <div class="comment-header">
                            <span class="comment-author">${escapeHtml(c.author)}</span>
                            <span class="comment-date">${timeAgo(c.created_at)}</span>
                        </div>
                        <div class="comment-body">${escapeHtml(c.body)}</div>
                    </div>
                </div>
            `).join("");
        }

        document.getElementById("comment-input").value = "";
        document.getElementById("comment-error").style.display = "none";

        const deleteBtn = document.getElementById("btn-delete-suggestion");
        if (s.is_owner) deleteBtn.classList.remove("hidden");
        else deleteBtn.classList.add("hidden");

        modal.style.display = "flex";
    } catch (e) {
        logger.error("Failed to load suggestion:", e);
    }
}
