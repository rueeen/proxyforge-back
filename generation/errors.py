def short_error(error):
    message = str(error)
    return message if len(message) <= 500 else message[:499] + '…'
