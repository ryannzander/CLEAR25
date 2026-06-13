/* ============================================================
   PM2.5 EWS — Feedback actions: vote, delete, submit, comment
   ============================================================ */

async function voteSuggestion(value) {
    if (!feedbackAuth.authenticated) {
        document.getElementById("modal-login").style.display = "flex";
        return;
    }
    if (!currentSuggestionId) return;

    const upBtn = document.getElementById("detail-upvote");
    const downBtn = document.getElementById("detail-downvote");
    const scoreEl = document.getElementById("detail-score");

    const wasVoted = (value === 1 && upBtn.classList.contains("voted-up")) ||
                     (value === -1 && downBtn.classList.contains("voted-down"));
    const newValue = wasVoted ? 0 : value;

    try {
        const resp = await fetch(`/api/suggestions/${currentSuggestionId}/vote/`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: newValue }),
        });
        const data = await resp.json();
        if (resp.ok) {
            scoreEl.textContent = data.score;
            upBtn.classList.toggle("voted-up", data.user_vote === 1);
            downBtn.classList.toggle("voted-down", data.user_vote === -1);
            loadSuggestions();
        }
    } catch (e) {
        logger.error("Vote failed:", e);
    }
}

async function deleteSuggestion() {
    if (!currentSuggestionId) return;

    if (!confirm("Are you sure you want to delete this suggestion? This cannot be undone.")) {
        return;
    }

    try {
        const resp = await fetch(`/api/suggestions/${currentSuggestionId}/delete/`, {
            method: "DELETE",
        });

        if (resp.ok) {
            document.getElementById("modal-detail").style.display = "none";
            loadSuggestions();
        } else {
            alert("Failed to delete suggestion");
        }
    } catch (e) {
        logger.error("Delete failed:", e);
        alert("Failed to delete suggestion");
    }
}

async function submitSuggestion() {
    const title = document.getElementById("suggestion-title").value.trim();
    const body = document.getElementById("suggestion-body").value.trim();
    const errorEl = document.getElementById("suggestion-error");

    if (!title || title.length < 5) {
        errorEl.textContent = "Title must be at least 5 characters";
        errorEl.style.display = "block";
        return;
    }
    if (!body || body.length < 10) {
        errorEl.textContent = "Description must be at least 10 characters";
        errorEl.style.display = "block";
        return;
    }

    try {
        const resp = await fetch("/api/suggestions/create/", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title, body }),
        });
        const data = await resp.json();

        if (!resp.ok) {
            errorEl.textContent = data.error || "Failed to create suggestion";
            errorEl.style.display = "block";
            return;
        }

        document.getElementById("modal-suggestion").style.display = "none";
        loadSuggestions();
    } catch (e) {
        errorEl.textContent = "Network error";
        errorEl.style.display = "block";
    }
}

async function addComment() {
    if (!feedbackAuth.authenticated) {
        document.getElementById("modal-login").style.display = "flex";
        return;
    }
    if (!currentSuggestionId) return;

    const input = document.getElementById("comment-input");
    const body = input.value.trim();
    const errorEl = document.getElementById("comment-error");

    if (!body || body.length < 2) {
        errorEl.textContent = "Comment must be at least 2 characters";
        errorEl.style.display = "block";
        return;
    }

    try {
        const resp = await fetch(`/api/suggestions/${currentSuggestionId}/comments/`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ body }),
        });
        const data = await resp.json();

        if (!resp.ok) {
            errorEl.textContent = data.error || "Failed to add comment";
            errorEl.style.display = "block";
            return;
        }

        const commentsEl = document.getElementById("detail-comments");
        const emptyMsg = commentsEl.querySelector(".comments-empty");
        if (emptyMsg) emptyMsg.remove();

        commentsEl.insertAdjacentHTML("beforeend", `
            <div class="comment-item">
                <img src="${escapeHtml(data.author_avatar)}" alt="" class="comment-avatar">
                <div class="comment-content">
                    <div class="comment-header">
                        <span class="comment-author">${escapeHtml(data.author)}</span>
                        <span class="comment-date">just now</span>
                    </div>
                    <div class="comment-body">${escapeHtml(data.body)}</div>
                </div>
            </div>
        `);

        input.value = "";
        errorEl.style.display = "none";
        loadSuggestions();
    } catch (e) {
        errorEl.textContent = "Network error";
        errorEl.style.display = "block";
    }
}
