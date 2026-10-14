"""Public error vocabulary; never relay subprocess output or input secrets."""
class ManagementError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def public_error(error: Exception) -> dict:
    if isinstance(error, ManagementError):
        return {'code': error.code, 'message': error.message}
    text = str(error).lower()
    if 'stale' in text or 'changed' in text or 'already applied' in text:
        return {'code':'STALE_REVISION','message':'配置或文件已变化，请刷新后重新预览。'}
    if 'conflicting authorization' in text:
        return {'code':'CONFLICT','message':'相同公钥已有不同的授权限制，已阻止追加。请通过独立操作处理原限制。'}
    if 'interrupted activation' in text or 'another configuration' in text:
        return {'code':'BUSY','message':'存在进行中或中断的变更。请检查任务，并在需要时恢复部署。'}
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return {'code':'VALIDATION','message':'校验未通过。请检查字段格式、证书域名和客户端覆盖选项。现有配置未提交。'}
    return {'code':'OPERATION_FAILED','message':'操作未完成。请检查服务状态和恢复记录；不要重复执行旧计划。'}
