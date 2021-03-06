from dateutil.relativedelta import relativedelta
from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import ugettext_lazy as _
from django.views.generic import DetailView, FormView, ListView, View

from byro.bookkeeping.models import Booking
from byro.bookkeeping.special_accounts import SpecialAccounts
from byro.common.models import Configuration
from byro.members.forms import CreateMemberForm
from byro.members.models import Member, Membership
from byro.members.signals import (
    leave_member, leave_member_mail_information,
    leave_member_office_mail_information, new_member,
    new_member_mail_information, new_member_office_mail_information,
)
from byro.office.signals import member_view


class MemberView(DetailView):
    context_object_name = 'member'
    model = Member

    def get_member(self):
        return Member.all_objects.get(pk=self.kwargs.get('pk'))

    def get_queryset(self):
        return Member.all_objects.all()

    def get_context_data(self, *args, **kwargs):
        ctx = super().get_context_data(*args, **kwargs)
        responses = [r[1] for r in member_view.send_robust(self.get_object(), request=self.request)]
        ctx['member_views'] = responses
        ctx['member'] = self.get_member()
        return ctx


class MemberListView(ListView):
    template_name = 'office/member/list.html'
    context_object_name = 'members'
    model = Member
    paginate_by = 50

    def get_queryset(self):
        search = self.request.GET.get('q')
        _filter = self.request.GET.get('filter', 'active')
        qs = Member.objects.all()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(number=search))
        # Logic:
        #  + Active members have membership with start <= today and (end is null or end >= today)
        active_q = Q(memberships__start__lte=now().date()) & (Q(memberships__end__isnull=True) | Q(memberships__end__gte=now().date()))
        inactive_q = ~active_q
        if _filter == 'all':
            pass
        elif _filter == 'inactive':
            qs = qs.filter(inactive_q)
        else:  # Default to 'active'
            qs = qs.filter(active_q)
        return qs.order_by('-id').distinct()

    def post(self, request, *args, **kwargs):
        for member in Member.objects.all():
            member.update_liabilites()
        return redirect(request.path)


class MemberCreateView(FormView):
    template_name = 'office/member/add.html'
    form_class = CreateMemberForm

    def get_object(self):
        return Member.objects.get(pk=self.kwargs['pk'])

    @transaction.atomic
    def form_valid(self, form):
        self.form = form
        form.save()
        messages.success(self.request, _('The member was added, please edit additional details if applicable.'))
        form.instance.log(self, '.created')

        responses = new_member.send_robust(sender=form.instance)
        for module, response in responses:
            if isinstance(response, Exception):
                messages.warning(self.request, _('Some post processing steps could not be completed: ') + str(response))
        config = Configuration.get_solo()

        if config.welcome_member_template and form.instance.email:
            context = {
                'name': config.name,
                'contact': config.mail_from,
                'number': form.instance.number,
                'member_name': form.instance.name,
            }
            responses = [r[1] for r in new_member_mail_information.send_robust(sender=form.instance) if r]
            context['additional_information'] = '\n'.join(responses).strip()
            config.welcome_member_template.to_mail(email=form.instance.email, context=context)
        if config.welcome_office_template:
            context = {'member_name': form.instance.name}
            responses = [r[1] for r in new_member_office_mail_information.send_robust(sender=form.instance) if r]
            context['additional_information'] = '\n'.join(responses).strip()
            config.welcome_office_template.to_mail(email=config.backoffice_mail, context=context)
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('office:members.data', kwargs={'pk': self.form.instance.pk})


class MemberDashboardView(MemberView):
    template_name = 'office/member/dashboard.html'

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        obj = self.get_object()
        if not obj.memberships.count():
            return context
        first = obj.memberships.first().start
        delta = now().date() - first
        context['member_since'] = {
            'days': int(delta.total_seconds() / (60 * 60 * 24)),
            'years': round(delta.days / 365, 1),
            'first': first,
        }
        context['current_membership'] = {
            'amount': obj.memberships.last().amount,
            'interval': obj.memberships.last().get_interval_display()
        }
        context['statute_barred_debt'] = {
            'now': obj.statute_barred_debt(),
        }
        context['statute_barred_debt']['in1year'] = obj.statute_barred_debt(relativedelta(years=1)) - context['statute_barred_debt']['now']
        return context


class MemberDataView(MemberView):
    template_name = 'office/member/data.html'

    def _instantiate(self, form_class, member, profile_class=None, instance=None, prefix=None, empty=False):
        params = {
            'instance': (getattr(member, profile_class._meta.get_field('member').related_query_name()) if profile_class else instance) if not empty else None,
            'prefix': prefix or (profile_class.__name__ if profile_class else instance.__class__.__name__ + '_' if instance else 'member_'),
            'data': self.request.POST if self.request.method == 'POST' else None,
        }
        return form_class(**params)

    def get_forms(self):
        obj = self.get_object()
        membership_create_form = forms.modelform_factory(Membership, fields=['start', 'end', 'interval', 'amount'])
        for key in membership_create_form.base_fields:
            setattr(membership_create_form.base_fields[key], 'required', False)
        return [
            self._instantiate(forms.modelform_factory(Member, exclude=['membership_type']), member=obj, instance=obj),
        ] + [
            self._instantiate(forms.modelform_factory(Membership, exclude=['member']), member=obj, instance=m, prefix=m.id)
            for m in obj.memberships.all()
        ] + [self._instantiate(membership_create_form, member=obj, profile_class=Membership, empty=True)] + [
            self._instantiate(forms.modelform_factory(
                profile_class,
                fields=[f.name for f in profile_class._meta.fields if f.name not in ['id', 'member']],
            ), member=obj, profile_class=profile_class)
            for profile_class in obj.profile_classes
        ]

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context['forms'] = self.get_forms()
        return context

    @transaction.atomic
    def post(self, *args, **kwargs):
        any_changed = False
        for form in self.get_forms():
            if form.is_valid() and form.has_changed():
                if not getattr(form.instance, 'member', False):
                    form.instance.member = self.get_object()
                any_changed = True
                form.save()
        if any_changed:
            self.get_object().log(self, '.updated')
            messages.success(self.request, _('Your changes have been saved.'))
        return redirect(reverse('office:members.data', kwargs=self.kwargs))


class MemberFinanceView(MemberView):
    template_name = 'office/member/finance.html'
    paginate_by = 50

    def get_bookings(self):
        account_list = [SpecialAccounts.donations, SpecialAccounts.fees_receivable]
        return Booking.objects.with_transaction_data().filter(
            Q(debit_account__in=account_list) |
            Q(credit_account__in=account_list),
            member=self.get_member(),
            transaction__value_datetime__lte=now(),
        ).order_by('-transaction__value_datetime', '-booking_datetime', '-transaction__booking_datetime')

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context['member'] = self.get_member()
        context['bookings'] = self.get_bookings()
        return context


class MemberLeaveView(MemberView, FormView):
    template_name = 'office/member/leave.html'
    form_class = forms.modelform_factory(Membership,
                                         fields=['start', 'end', 'interval', 'amount'])

    def get_forms(self):
        obj = self.get_object()
        return [
            self.form_class(instance=m, prefix=m.id,
                            data=self.request.POST if self.request.method == 'POST' else None)
            for m in obj.memberships.all().order_by('-start')
        ]

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context['forms'] = self.get_forms()
        return context

    @transaction.atomic
    def post(self, *args, **kwargs):
        for form in self.get_forms():
            if form.is_valid() and form.has_changed() and form.instance.end:
                if not getattr(form.instance, 'member', False):
                    form.instance.member = self.get_object()

                form.save()
                form.instance.log(self, '.ended')
                messages.success(self.request, _('The membership has been terminated. Please check the outbox for the notifications.'))

                form.instance.member.update_liabilites()

                responses = leave_member.send_robust(sender=form.instance)
                for module, response in responses:
                    if isinstance(response, Exception):
                        messages.warning(self.request, _('Some post processing steps could not be completed: ') + str(response))

                config = Configuration.get_solo()
                if config.leave_member_template:
                    context = {
                        'name': config.name,
                        'contact': config.mail_from,
                        'number': form.instance.member.number,
                        'member_name': form.instance.member.name,
                        'end': form.instance.end,
                    }
                    responses = [r[1] for r in leave_member_mail_information.send_robust(sender=form.instance) if r]
                    context['additional_information'] = '\n'.join(responses).strip()
                    config.leave_member_template.to_mail(email=form.instance.member.email, context=context)
                if config.leave_office_template:
                    context = {
                        'member_name': form.instance.member.name,
                        'end': form.instance.end,
                    }
                    responses = [r[1] for r in leave_member_office_mail_information.send_robust(sender=form.instance) if r]
                    context['additional_information'] = '\n'.join(responses).strip()
                    config.leave_office_template.to_mail(email=config.backoffice_mail, context=context)
        return redirect(reverse('office:members.leave', kwargs=self.kwargs))


class MemberListTypeaheadView(View):

    def dispatch(self, request, *args, **kwargs):
        search = request.GET.get('search')
        if not search or len(search) < 2:
            return JsonResponse({'count': 0, 'results': []})

        queryset = Member.objects.filter(
            Q(name__icontains=search) | Q(profile_profile__nick__icontains=search)
        )
        return JsonResponse({
            'count': len(queryset),
            'results': [
                {
                    'id': member.pk,
                    'nick': member.profile_profile.nick,
                    'name': member.name,
                }
                for member in queryset
            ],
        })


class MemberRecordDisclosureView(MemberView):
    template_name = 'office/member/data_disclosure.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['mail'] = self.get_member().record_disclosure_email
        return ctx

    def post(self, request, *args, **kwargs):
        self.get_member().record_disclosure_email.save()
        self.get_member().log(self, '.disclosure_email_generated')
        messages.success(request, _('The email was generated and can be sent in the outbox.'))
        return redirect(reverse('office:members.dashboard', kwargs=self.kwargs))


class MemberLogView(MemberView):
    template_name = 'office/member/log.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['log_entries'] = self.get_member().log_entries()
        return ctx
