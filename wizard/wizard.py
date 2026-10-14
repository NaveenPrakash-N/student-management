from odoo import models, fields


class SendStudentEmail(models.TransientModel):
    _name = 'send.student.email'
    _description = 'Send Student Email'

    student_id = fields.Many2one('students.student', string='Student')
    email_from = fields.Char(string='From')
    email_to = fields.Char(string='To')
    email_subject = fields.Char(string='Subject')
    email_body = fields.Text(string='Body')

    def send_email(self):
        vals = {
            'subject': self.email_subject,
            'body_html': self.email_body,
            'email_from': self.email_from,
            'email_to': self.email_to,
        }
        mail = self.env['mail.mail'].create(vals)
        mail.send()
        return {
            'effect': {
                'fadeout': 'slow',
                'message': 'Mail Sent Successfully!',
                'type': 'rainbow_man',
            }
        }
        # return {
        #     'type': 'ir.actions.client',
        #     'tag': 'display_notification',
        #     'params': {
        #         'title': "Success",
        #         'message': "Email sent!",
        #         'type': 'success',
        #         'sticky': False,
        #     }
        # }