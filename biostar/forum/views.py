
from django.shortcuts import render, redirect, reverse
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.contrib.auth import get_user_model

from biostar.accounts.models import Profile
from . import forms, auth
from .models import Post, Vote, Message
from .decorators import object_exists, message_access
from .const import *
User = get_user_model()


def list_view(request, template="post_list.html", extra_context={}, topic=None,
              extra_proc=lambda x:x, per_page=20):
    "List view for posts and messages"

    topic = topic or request.GET.get("topic", 'latest')
    page = request.GET.get('page')

    is_private_topic = topic not in ("latest", "community")
    is_public_topic = topic in ("latest", "community")

    if request.user.is_anonymous and is_private_topic:
        messages.error(request, f"You must be logged in to view that topic.")

        topic, template = "latest", "post_list.html"

    objs = auth.list_by_topic(request=request, topic=topic).order_by("-pk")

    if is_public_topic:
        # Projects not shown when looking at public topics
        objs = objs.filter(project=None)

    # Apply extra protocols to queryset (updates, etc)
    objs = extra_proc(objs)

    # Get the page info
    paginator = Paginator(objs, per_page)
    objs = paginator.get_page(page)

    context = dict(objs=objs)
    context.update(extra_context)

    return render(request, template_name=template, context=context)


def list_by_topic(request, topic):
    #TODO: going to take out when refractoring
    "Used to keep track of topics when going fr"

    return list_view(request=request, topic=topic)



@login_required
def message_list(request):

    active_tab = request.GET.get(ACTIVE_TAB, INBOX)

    active_tab = active_tab if (active_tab in MESSAGE_TABS) else INBOX

    context = {active_tab: ACTIVE_TAB, "not_outbox": active_tab != OUTBOX}

    user = request.user

    Profile.objects.filter(user=user).update(new_messages=0)

    msg_per_page = 20

    return list_view(request, template="message_list.html",
                     topic=active_tab, extra_context=context, per_page=msg_per_page)


def community_list(request):

    # Users that make posts or votes are
    # considered part of the community

    users_per_page = 50
    template = "community_list.html"
    topic = "community"

    return list_view(request=request, template=template, per_page=users_per_page,
                     topic=topic)


@object_exists(klass=Message, url="message_list")
@message_access
def message_view(request, uid):

    base_message = Message.objects.filter(uid=uid).first()

    # Build the message tree from bottom up
    tree = auth.build_msg_tree(msg=base_message, tree=[])

    # Update the unread flag
    Message.objects.filter(pk=base_message.pk).update(unread=False)

    context = dict(message=base_message, tree=tree)

    return render(request, "message_view.html", context=context)


@object_exists(klass=Post)
@login_required
def update_vote(request, uid, next=None):

    # Post to upvote/bookmark
    post = Post.objects.filter(uid=uid).first()
    user = request.user
    vmap = {"upvote": Vote.UP, "bookmark": Vote.BOOKMARK}

    vote_type = vmap.get(request.GET.get("type"), Vote.EMPTY)

    vote = Vote.objects.filter(post=post, author=user, type=vote_type).first()
    next_url = request.GET.get(REDIRECT_FIELD_NAME,
                               request.POST.get(REDIRECT_FIELD_NAME))
    next_url = next or next_url or "/"

    if vote:
        # Change vote to empty if clicked twice
        auth.create_vote(update=True, author=user, post=post, vote_type=vote.type,
                         updated_type=Vote.EMPTY)
    elif not vote:
        auth.create_vote(author=user, post=post, vote_type=vote_type)

    return redirect(next_url)


@object_exists(klass=Post)
def post_view(request, uid, template="post_view.html", url="post_view",
              extra_context={}, project=None):
    "Return a detailed view for specific post"

    # Form used for answers
    form = forms.PostShortForm()

    # Get the parents info
    obj = Post.objects.filter(uid=uid).first()

    # Return root view if not at top level.
    obj = obj if obj.is_toplevel else obj.root

    # Update the post views.
    Post.update_post_views(obj, request=request)

    if request.method == "POST":
        form = forms.PostShortForm(data=request.POST)
        if form.is_valid():
            form.save(parent=obj.parent, author=request.user, project=project)
            return redirect(reverse(url, kwargs=dict(uid=obj.root.uid)))

    # Adds the permissions
    obj = auth.post_permissions(request=request, post=obj)

    # Populate the object to build a tree that contains all posts in the thread.
    # Answers are added here as well.
    obj = auth.build_obj_tree(request=request, obj=obj)

    context = dict(post=obj, form=form)
    context.update(extra_context)

    return render(request, template, context=context)


@login_required
def post_comment(request, uid, template="post_comment.html", url="post_view", extra_context={},
                 project=None):
    """Used to structure a comment for viewing in biostar.engine and biostar.forum """

    # Get the parent post to add comment to
    obj = Post.objects.filter(uid=uid).first()

    # Form used for answers
    form = forms.PostShortForm()

    if request.method == "POST":

        form = forms.PostShortForm(data=request.POST)

        if form.is_valid():
            form = forms.PostShortForm(data=request.POST)
            if form.is_valid():
                form.save(parent=obj, author=request.user, post_type=Post.COMMENT,
                          project=project)
            return redirect(reverse(url, kwargs=dict(uid=obj.root.uid)))

    context = dict(form=form, post=obj)
    context.update(extra_context)

    return render(request, template, context=context)


@object_exists(klass=Post)
@login_required
def subs_action(request, uid, next=None):

    # Post actions are being taken on
    post = Post.objects.filter(uid=uid).first()
    user = request.user
    next_url = request.GET.get(REDIRECT_FIELD_NAME,
                               request.POST.get(REDIRECT_FIELD_NAME))
    next_url = next or next_url or "/"

    if request.method == "POST" and user.is_authenticated:
        form = forms.SubsForm(data=request.POST, post=post, user=user)

        if form.is_valid():
            sub = form.save()
            msg = f"Updated Subscription to : {sub.get_type_display()}"
            messages.success(request, msg)

    return redirect(next_url)


@login_required
def post_create(request, project=None, template="post_create.html", url="post_view",
                extra_context={}, filter_func=lambda x: x):
    "Make a new post"

    # Filter function ( filter_func ) is used to filter choices from the form
    # between sites.
    form = forms.PostLongForm(project=project, filter_func=filter_func)

    if request.method == "POST":
        form = forms.PostLongForm(data=request.POST, project=project, filter_func=filter_func)
        if form.is_valid():
            # Create a new post by user
            post = form.save(author=request.user)
            return redirect(reverse(url, kwargs=dict(uid=post.uid)))

    context = dict(form=form)
    context.update(extra_context)

    return render(request, template, context=context)


@object_exists(klass=Post)
@login_required
def edit_post(request, uid):
    "Edit an existing post"

    return







