from gazette.mapeadores.base.mapeador import Mapeador


class MapeadorAgape(Mapeador):
    name = "mapeadoragape"

    def pattern_name(self):
        return "AGAPE"

    def valid_urls(self):
        return "vAGAPE"

    def urls_pattern(self, protocol, city, state_code):
        # casos conhecidos
        # https://agportal.agapesistemas.com.br/DiarioOficial/?alias=pmRIACHUELO
        # https://agportal.agapesistemas.com.br/DiarioOficial/?alias=pmcumbe

        return [
            f"{protocol}://agportal.agapesistemas.com.br/DiarioOficial/?alias=pm{city}"
        ]

    def validation(self, response):
        if "agapesistemas.com.br" in response.text:
            if 'div class="item"' in response.text or "bandeira" in response.text:
                return True
        return False
