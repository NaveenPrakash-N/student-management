from odoo import models, fields


class StudentTeacher(models.Model):
    _name = 'students.teacher'
    _description = 'Teacher'

    name = fields.Char(string='Name', required=True)
    phone = fields.Char(string='Phone')
    email = fields.Char(string='Email')
    user_id = fields.Many2one('res.users', string='Login User')
    student_ids = fields.One2many('students.student', 'teacher_id', string='Students')