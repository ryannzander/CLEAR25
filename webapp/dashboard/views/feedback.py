"""
Feedback board views: suggestions, votes, comments.
"""

from datetime import datetime

from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ..models import Suggestion, SuggestionVote, Comment
from .utils import (
    MAX_TITLE_LENGTH, MAX_BODY_LENGTH, MAX_COMMENT_LENGTH,
    sanitize_text, validate_json_body, validate_id,
    contains_profanity, serialize_author,
)


def api_suggestions(request):
    """List all suggestions with vote counts and comment counts.

    Optimized: Uses annotations to compute scores in a single query.
    """
    sort = request.GET.get("sort", "hot")

    suggestions = Suggestion.objects.select_related("author").annotate(
        upvotes=Count('votes', filter=Q(votes__value=1)),
        downvotes=Count('votes', filter=Q(votes__value=-1)),
        num_comments=Count('comments'),
    ).all()

    user_votes = {}
    if request.user.is_authenticated:
        user_votes = {v.suggestion_id: v.value for v in SuggestionVote.objects.filter(user=request.user)}

    items = []
    for s in suggestions:
        score = s.upvotes - s.downvotes
        items.append({
            "id": s.id,
            "title": s.title,
            "body": s.body,
            **serialize_author(s.author),
            "created_at": s.created_at.isoformat(),
            "score": score,
            "comment_count": s.num_comments,
            "user_vote": user_votes.get(s.id, 0),
        })

    # Sort
    if sort == "new":
        items.sort(key=lambda x: x["created_at"], reverse=True)
    elif sort == "top":
        items.sort(key=lambda x: x["score"], reverse=True)
    else:  # hot
        now = timezone.now()
        for item in items:
            age_hours = (now - datetime.fromisoformat(item["created_at"])).total_seconds() / 3600
            item["_hot"] = item["score"] / (age_hours + 2) ** 1.5
        items.sort(key=lambda x: x["_hot"], reverse=True)
        for item in items:
            del item["_hot"]

    return JsonResponse({"suggestions": items})


@require_http_methods(["POST"])
def api_suggestion_create(request):
    """Create a new suggestion. Requires authentication."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    data, error = validate_json_body(request)
    if error:
        return error

    title = sanitize_text(data.get("title", ""), MAX_TITLE_LENGTH)
    body = sanitize_text(data.get("body", ""), MAX_BODY_LENGTH)

    if not title or len(title) < 5:
        return JsonResponse({"error": "Title must be at least 5 characters"}, status=400)
    if len(title) > MAX_TITLE_LENGTH:
        return JsonResponse({"error": f"Title must be at most {MAX_TITLE_LENGTH} characters"}, status=400)
    if not body or len(body) < 10:
        return JsonResponse({"error": "Description must be at least 10 characters"}, status=400)
    if len(body) > MAX_BODY_LENGTH:
        return JsonResponse({"error": f"Description must be at most {MAX_BODY_LENGTH} characters"}, status=400)

    if contains_profanity(title) or contains_profanity(body):
        return JsonResponse({"error": "Please keep it professional — no profanity allowed"}, status=400)

    s = Suggestion.objects.create(author=request.user, title=title, body=body)
    return JsonResponse({
        "id": s.id,
        "title": s.title,
        "body": s.body,
        **serialize_author(s.author),
        "created_at": s.created_at.isoformat(),
        "score": 0,
        "comment_count": 0,
    })


@require_http_methods(["POST"])
def api_suggestion_vote(request, suggestion_id):
    """Vote on a suggestion. Requires authentication."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    sid, error = validate_id(suggestion_id, "suggestion_id")
    if error:
        return error

    data, error = validate_json_body(request)
    if error:
        return error

    value = data.get("value", 0)
    if not isinstance(value, int) or value not in (-1, 0, 1):
        return JsonResponse({"error": "Value must be -1, 0, or 1"}, status=400)

    try:
        suggestion = Suggestion.objects.get(id=sid)
    except Suggestion.DoesNotExist:
        return JsonResponse({"error": "Suggestion not found"}, status=404)

    if value == 0:
        SuggestionVote.objects.filter(user=request.user, suggestion=suggestion).delete()
    else:
        SuggestionVote.objects.update_or_create(
            user=request.user, suggestion=suggestion,
            defaults={"value": value}
        )

    return JsonResponse({"score": suggestion.vote_score(), "user_vote": value})


def api_suggestion_detail(request, suggestion_id):
    """Get a suggestion with all its comments.

    Optimized: Uses annotations for vote score and prefetch for comments.
    """
    try:
        s = Suggestion.objects.select_related("author").prefetch_related(
            "comments__author"
        ).annotate(
            upvotes=Count('votes', filter=Q(votes__value=1)),
            downvotes=Count('votes', filter=Q(votes__value=-1)),
        ).get(id=suggestion_id)
    except Suggestion.DoesNotExist:
        return JsonResponse({"error": "Suggestion not found"}, status=404)

    user_vote = 0
    if request.user.is_authenticated:
        vote = SuggestionVote.objects.filter(user=request.user, suggestion=s).first()
        if vote:
            user_vote = vote.value

    comments = [{
        "id": c.id,
        "body": c.body,
        **serialize_author(c.author),
        "created_at": c.created_at.isoformat(),
    } for c in s.comments.all()]

    is_owner = request.user.is_authenticated and request.user == s.author
    score = s.upvotes - s.downvotes

    return JsonResponse({
        "id": s.id,
        "title": s.title,
        "body": s.body,
        **serialize_author(s.author),
        "created_at": s.created_at.isoformat(),
        "score": score,
        "user_vote": user_vote,
        "comments": comments,
        "is_owner": is_owner,
    })


@require_http_methods(["POST"])
def api_comment_create(request, suggestion_id):
    """Add a comment to a suggestion. Requires authentication."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    sid, error = validate_id(suggestion_id, "suggestion_id")
    if error:
        return error

    try:
        suggestion = Suggestion.objects.get(id=sid)
    except Suggestion.DoesNotExist:
        return JsonResponse({"error": "Suggestion not found"}, status=404)

    data, error = validate_json_body(request)
    if error:
        return error

    body = sanitize_text(data.get("body", ""), MAX_COMMENT_LENGTH)

    if not body or len(body) < 2:
        return JsonResponse({"error": "Comment must be at least 2 characters"}, status=400)
    if len(body) > MAX_COMMENT_LENGTH:
        return JsonResponse({"error": f"Comment must be at most {MAX_COMMENT_LENGTH} characters"}, status=400)

    if contains_profanity(body):
        return JsonResponse({"error": "Please keep it professional — no profanity allowed"}, status=400)

    c = Comment.objects.create(author=request.user, suggestion=suggestion, body=body)
    return JsonResponse({
        "id": c.id,
        "body": c.body,
        **serialize_author(c.author),
        "created_at": c.created_at.isoformat(),
    })


@require_http_methods(["DELETE"])
def api_suggestion_delete(request, suggestion_id):
    """Delete a suggestion. Only the author can delete."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    sid, error = validate_id(suggestion_id, "suggestion_id")
    if error:
        return error

    try:
        suggestion = Suggestion.objects.get(id=sid)
    except Suggestion.DoesNotExist:
        return JsonResponse({"error": "Suggestion not found"}, status=404)

    if suggestion.author != request.user:
        return JsonResponse({"error": "You can only delete your own suggestions"}, status=403)

    suggestion.delete()
    return JsonResponse({"ok": True})
