from odoo import models, fields, api


class StudentMark(models.Model):
    _name = 'students.mark'
    _description = 'Student Mark'

    student_id = fields.Many2one('students.student', string='Student', required=True, ondelete='cascade')
    subject_id = fields.Many2one('students.subject', string='Subject', required=True)
    marks_obtained = fields.Float(string='Marks Obtained', required=True)
    total_mark = fields.Float(string='Total Mark', required=True, default=100)
    percentage = fields.Float(string='Percentage', compute='_compute_percentage', store=True)
    grade = fields.Selection([
        ('o', 'O'),
        ('a+', 'A+'),
        ('a', 'A'),
        ('b+', 'B+'),
        ('b', 'B'),
        ('c', 'C'),
        ('f', 'F'),
    ], string='Grade', compute='_compute_grade', store=True)
    result = fields.Selection([
        ('pass', 'Pass'),
        ('fail', 'Fail'),
    ], string='Result', compute='_compute_result', store=True)

    _unique_student_subject = models.Constraint(
        'unique(student_id, subject_id)',
        'Marks for this subject are already added for this student!'
    )

    @api.depends('marks_obtained', 'total_mark')
    def _compute_percentage(self):
        for record in self:
            record.percentage = (record.marks_obtained / record.total_mark * 100) if record.total_mark else 0

    @api.depends('percentage')
    def _compute_grade(self):
        for record in self:
            p = record.percentage
            if p >= 90:
                record.grade = 'o'
            elif p >= 80:
                record.grade = 'a+'
            elif p >= 70:
                record.grade = 'a'
            elif p >= 60:
                record.grade = 'b+'
            elif p >= 50:
                record.grade = 'b'
            elif p >= 40:
                record.grade = 'c'
            else:
                record.grade = 'f'

    @api.depends('percentage')
    def _compute_result(self):
        for record in self:
            record.result = 'pass' if record.percentage >= 40 else 'fail'