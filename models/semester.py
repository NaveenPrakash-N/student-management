from odoo import models, fields, api


class StudentSemester(models.Model):
    _name = 'students.semester'
    _description = 'Student Semester'
    _order = 'semester asc'
    _rec_name = 'display_name'

    semester = fields.Selection([
        ('1', 'Semester 1'),
        ('2', 'Semester 2'),
        ('3', 'Semester 3'),
        ('4', 'Semester 4'),
        ('5', 'Semester 5'),
        ('6', 'Semester 6'),
        ('7', 'Semester 7'),
        ('8', 'Semester 8'),
    ], string='Semester', required=True)
    display_name = fields.Char(string='Name', compute='_compute_display_name', store=True)
    fee_type_ids = fields.Many2many('students.fee.type', string='Fee Structure')
    total_amount = fields.Float(string='Total Amount', compute='_compute_total_amount', store=True)

    _unique_semester = models.Constraint('unique(semester)', 'This semester already exists!')

    @api.depends('semester')
    def _compute_display_name(self):
        for record in self:
            record.display_name = dict(self._fields['semester'].selection).get(record.semester, '')

    @api.depends('fee_type_ids.amount')
    def _compute_total_amount(self):
        for record in self:
            record.total_amount = sum(record.fee_type_ids.mapped('amount'))


class StudentSemesterFee(models.Model):
    _name = 'students.semester.fee'
    _description = 'Student Semester Fee'

    student_id = fields.Many2one('students.student', string='Student', required=True, ondelete='cascade')
    semester_id = fields.Many2one('students.semester', string='Semester', required=True)
    payable_amount = fields.Float(string='Payable Amount')
    paid_amount = fields.Float(string='Paid Amount')
    balance = fields.Float(string='Balance', compute='_compute_balance', store=True)
    status = fields.Selection([
        ('unpaid', 'Unpaid'),
        ('partial', 'Partial Paid'),
        ('paid', 'Paid'),
    ], string='Status', compute='_compute_status', store=True)

    discount_percent = fields.Float(string='Discount %', related='student_id.discount_percent', store=True)
    discounted_price = fields.Float(string='Discounted Price', compute='_compute_discounted_price', store=True)

    _unique_student_semester = models.Constraint(
        'unique(student_id, semester_id)',
        'This semester is already added for this student!'
    )

    @api.depends('payable_amount', 'discount_percent')
    def _compute_discounted_price(self):
        for record in self:
            record.discounted_price = record.payable_amount - (record.payable_amount * record.discount_percent / 100)

    @api.depends('discounted_price', 'paid_amount')
    def _compute_balance(self):
        for record in self:
            record.balance = record.discounted_price - record.paid_amount

    @api.depends('paid_amount', 'discounted_price')
    def _compute_status(self):
        for record in self:
            if record.paid_amount <= 0:
                record.status = 'unpaid'
            elif record.paid_amount >= record.discounted_price:
                record.status = 'paid'
            else:
                record.status = 'partial'

    @api.onchange('semester_id')
    def _onchange_semester_id(self):
        if self.semester_id:
            self.payable_amount = self.semester_id.total_amount