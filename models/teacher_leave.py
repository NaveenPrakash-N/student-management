from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
import logging

_logger = logging.getLogger(__name__)


class TeacherLeave(models.Model):

    _name = 'students.teacher.leave'
    _description = 'Teacher Leave Request'
    _order = 'create_date desc'

    teacher_id = fields.Many2one(
        'students.teacher',
        string='Teacher',
        required=True,
        default=lambda self: self._get_current_teacher(),
    )

    substitute_teacher_id = fields.Many2one(
        'students.teacher',
        string='Substitute Teacher',
    )

    date_from = fields.Date(
        string='From Date',
        required=True,
    )

    date_to = fields.Date(
        string='To Date',
        required=True,
    )

    reason = fields.Text(
        string='Reason for Leave',
        required=True,
    )

    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('approved', 'Approved'),
            ('refused', 'Refused'),
        ],
        string='Status',
        default='draft',
        required=True,
        copy=False,
    )

    notified = fields.Boolean(
        string='Students Notified',
        default=False,
        copy=False,
    )

    # ---------------------------------------------------------
    # CURRENT TEACHER
    # ---------------------------------------------------------

    def _get_current_teacher(self):

        if self.env.user.has_group('base.group_system'):
            return False

        teacher = self.env['students.teacher'].sudo().search(
            [
                ('user_id', '=', self.env.uid)
            ],
            limit=1
        )

        return teacher.id if teacher else False

    # ---------------------------------------------------------
    # DATE VALIDATION
    # ---------------------------------------------------------

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):

        for record in self:

            if (
                record.date_from
                and record.date_to
                and record.date_to < record.date_from
            ):
                raise ValidationError(
                    'The end date cannot be before the start date.'
                )

    # ---------------------------------------------------------
    # CREATE
    # ---------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):

        is_admin = self.env.user.has_group(
            'base.group_system'
        )

        if not is_admin:

            teacher = self.env['students.teacher'].sudo().search(
                [
                    ('user_id', '=', self.env.uid)
                ],
                limit=1
            )

            if not teacher:

                raise AccessError(
                    'Your user account is not linked to a teacher.'
                )

            for vals in vals_list:
                vals['teacher_id'] = teacher.id

        else:

            for vals in vals_list:

                if not vals.get('teacher_id'):

                    raise ValidationError(
                        'Please select a teacher for this leave request.'
                    )

        return super().create(vals_list)

    # ---------------------------------------------------------
    # WRITE
    # ---------------------------------------------------------

    def write(self, vals):

        is_admin = self.env.user.has_group(
            'base.group_system'
        )

        if not is_admin:

            for record in self:

                if record.state != 'draft':

                    raise AccessError(
                        'You cannot modify a leave request after '
                        'it has been approved or refused.'
                    )

                if 'teacher_id' in vals:

                    raise AccessError(
                        'You cannot change the teacher of a '
                        'leave request.'
                    )

        return super().write(vals)

    # ---------------------------------------------------------
    # APPROVE
    # ---------------------------------------------------------

    def action_approve(self):

        if not self.env.user.has_group(
            'base.group_system'
        ):

            raise AccessError(
                'Only administrators can approve leave requests.'
            )

        for record in self:

            record.write({
                'state': 'approved',
                'notified': False,
            })

        return True

    # ---------------------------------------------------------
    # REFUSE
    # ---------------------------------------------------------

    def action_refuse(self):

        if not self.env.user.has_group(
            'base.group_system'
        ):

            raise AccessError(
                'Only administrators can refuse leave requests.'
            )

        for record in self:

            record.write({
                'state': 'refused',
            })

        return True

    # ---------------------------------------------------------
    # RESET TO DRAFT
    # ---------------------------------------------------------

    def action_reset_to_draft(self):

        if not self.env.user.has_group(
            'base.group_system'
        ):

            raise AccessError(
                'Only administrators can reset leave requests.'
            )

        for record in self:

            record.write({
                'state': 'draft',
                'notified': False,
            })

        return True

    # ---------------------------------------------------------
    # SEND EMAIL TO STUDENTS
    # ---------------------------------------------------------

    @api.model
    def _cron_notify_students_of_leave(self):

        _logger.info(
            "========== TEACHER LEAVE CRON STARTED =========="
        )

        leaves = self.search([
            ('state', '=', 'approved'),
            ('notified', '=', False),
        ])

        _logger.info(
            "Approved unnotified leave requests: %s",
            len(leaves)
        )

        template = self.env.ref(
            'students_management.mail_template_teacher_leave_notify',
            raise_if_not_found=False,
        )

        if not template:

            _logger.error(
                "Teacher leave email template NOT FOUND."
            )

            return

        _logger.info(
            "Teacher leave email template found: ID %s",
            template.id
        )

        for leave in leaves:

            _logger.info(
                "Processing leave ID=%s | Teacher=%s",
                leave.id,
                leave.teacher_id.name
            )

            students = self.env['students.student'].search([
                ('teacher_id', '=', leave.teacher_id.id),
                ('email', '!=', False),
            ])

            _logger.info(
                "Students found for teacher %s: %s",
                leave.teacher_id.name,
                len(students)
            )

            if not students:

                _logger.warning(
                    "No students with email found for teacher %s",
                    leave.teacher_id.name
                )

                continue

            all_success = True

            for student in students:

                _logger.info(
                    "Sending leave email to %s <%s>",
                    student.name,
                    student.email
                )

                try:

                    mail_id = template.with_context(

                        teacher_name=leave.teacher_id.name,

                        substitute_teacher_name=(
                            leave.substitute_teacher_id.name
                            if leave.substitute_teacher_id
                            else 'No substitute teacher assigned'
                        ),

                        leave_reason=leave.reason,

                        date_from=leave.date_from,

                        date_to=leave.date_to,

                    ).send_mail(
                        student.id,
                        force_send=True
                    )

                    mail = self.env['mail.mail'].browse(mail_id)

                    if mail.exists() and mail.state != 'sent':

                        all_success = False

                        _logger.error(
                            "Email was NOT sent to %s. Mail state: %s",
                            student.email,
                            mail.state
                        )

                    else:

                        _logger.info(
                            "Email successfully sent to %s",
                            student.email
                        )

                except Exception:

                    all_success = False

                    _logger.exception(
                        "FAILED to send leave email to %s <%s>",
                        student.name,
                        student.email
                    )

            if all_success:

                leave.write({
                    'notified': True,
                })

                _logger.info(
                    "Leave ID=%s marked as Students Notified.",
                    leave.id
                )

            else:

                _logger.warning(
                    "Leave ID=%s was NOT marked as notified "
                    "because one or more emails failed.",
                    leave.id
                )

        _logger.info(
            "========== TEACHER LEAVE CRON FINISHED =========="
        )