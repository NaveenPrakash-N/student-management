from odoo import models, fields, api
from odoo.exceptions import ValidationError
from datetime import date


class Student(models.Model):

    _name = 'students.student'
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = 'Student'

 
    name = fields.Char(
        string='Name',
        tracking=True
    )

    age = fields.Integer(
        string='Age',
        compute='_compute_age',
        store=True
    )

    dob = fields.Date(
        string='Date Of Birth',
        tracking=True
    )

    gender = fields.Selection(
        [
            ('male', 'Male'),
            ('female', 'Female'),
            ('other', 'Other')
        ],
        string='Gender',
        tracking=True
    )

    sequence = fields.Char(
        string='Sequence',
        copy=False,
        readonly=True
    )


    department_id = fields.Many2one(
        'students.department',
        string='Department',
        tracking=True
    )

    subject_ids = fields.Many2many(
        'students.subject',
        string='Subjects',
        tracking=True
    )

    teacher_id = fields.Many2one(
        'students.teacher',
        string='Teacher',
        tracking=True
    )


    semester_fee_ids = fields.One2many(
        'students.semester.fee',
        'student_id',
        string='Semester Fees'
    )

    phone = fields.Char(
        string='Phone',
        tracking=True
    )

    photo = fields.Image(
        string='Photo',
        max_width=1024,
        max_height=1024
    )

    address = fields.Char(
        string='Address',
        tracking=True
    )

    email = fields.Char(
        string='Email',
        tracking=True
    )

 
    enrollment_state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('confirmed', 'Confirmed'),
            ('completed', 'Completed'),
        ],
        string='Enrollment Status',
        default='draft',
        tracking=True
    )

    internal_note = fields.Text(
        string='Internal Note',
        tracking=True
    )


    mark_ids = fields.One2many(
        'students.mark',
        'student_id',
        string='Marks'
    )

    overall_percentage = fields.Float(
        string='Overall Percentage',
        compute='_compute_overall_percentage',
        store=True
    )

    discount_percent = fields.Float(
        string='Fee Discount %',
        compute='_compute_discount_percent',
        store=True
    )

 
    @api.depends('mark_ids.percentage')
    def _compute_overall_percentage(self):

        for record in self:

            marks = record.mark_ids

            record.overall_percentage = (
                sum(marks.mapped('percentage')) / len(marks)
                if marks
                else 0
            )


    @api.depends('overall_percentage')
    def _compute_discount_percent(self):

        for record in self:

            p = record.overall_percentage

            if p >= 90:
                record.discount_percent = 25

            elif p >= 80:
                record.discount_percent = 20

            elif p >= 70:
                record.discount_percent = 15

            elif p >= 60:
                record.discount_percent = 10

            elif p >= 50:
                record.discount_percent = 5

            else:
                record.discount_percent = 0


    def action_confirm(self):

        for record in self:
            record.enrollment_state = 'confirmed'

    def action_complete(self):

        for record in self:
            record.enrollment_state = 'completed'

    def action_reset_draft(self):

        for record in self:
            record.enrollment_state = 'draft'


    def action_print_report(self):

        return self.env.ref(
            'students_management.action_report_student'
        ).report_action(self)

    def _compute_semester_ids(self):

        all_semesters = self.env[
            'students.semester'
        ].search([])

        for record in self:
            record.semester_ids = all_semesters


    _sql_constraints = [
        (
            'unique_name_email',
            'unique(name, email)',
            'A student with this name and email already exists!'
        )
    ]

    @api.depends('dob')
    def _compute_age(self):

        for record in self:

            if record.dob:

                today = date.today()

                record.age = (
                    today.year
                    - record.dob.year
                    - (
                        (today.month, today.day)
                        < (record.dob.month, record.dob.day)
                    )
                )

            else:

                record.age = 0


    def send_email_wizard(self):

        action = self.env.ref(
            'students_management.send_student_email_action'
        ).read()[0]

        action['context'] = {
            'default_student_id': self.id,
            'default_email_to': self.email,
            'default_email_from': self.env.user.email,
        }

        return action

    @api.model_create_multi
    def create(self, vals_list):

        for vals in vals_list:

            existing = self.env[
                'students.student'
            ].search(
                [
                    ('name', '=', vals.get('name')),
                    ('email', '=', vals.get('email'))
                ],
                limit=1
            )

            if existing:

                raise ValidationError(
                    "Student with this name and email already exists."
                )

            vals['sequence'] = self.env[
                'ir.sequence'
            ].next_by_code(
                'students.student.sequence'
            )

        return super().create(vals_list)

    def write(self, vals):

        for record in self:

            name = vals.get(
                'name',
                record.name
            )

            email = vals.get(
                'email',
                record.email
            )

            existing = self.env[
                'students.student'
            ].search(
                [
                    ('name', '=', name),
                    ('email', '=', email),
                    ('id', '!=', record.id)
                ],
                limit=1
            )

            if existing:

                raise ValidationError(
                    "Student with this name and email already exists."
                )

        department_changed = 'department_id' in vals

        result = super().write(vals)

        if department_changed:

            self.env['bus.bus']._sendone(
                self.env.user.partner_id,
                'simple_notification',
                {
                    'type': 'success',
                    'title': "Department Updated",
                    'message': "Department changed successfully.",
                    'sticky': False,
                }
            )

        return result